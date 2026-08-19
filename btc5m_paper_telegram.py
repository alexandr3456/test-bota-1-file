#!/usr/bin/env python3
"""BTC 5m signal notifier and paper trader. This file has no order execution path."""
import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests

UTC = dt.timezone.utc
MOSCOW = dt.timezone(dt.timedelta(hours=3), name='MSK')
GAMMA = 'https://gamma-api.polymarket.com/events'
CLOB = 'https://clob.polymarket.com/book'
BYBIT = 'https://api.bybit.com/v5/market/kline'


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def bucket_5m(ts: int) -> int:
    return ts - ts % 300


def parse_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def fetch_market(slot: Optional[int] = None) -> Optional[dict[str, Any]]:
    slot = bucket_5m(int(time.time())) if slot is None else slot
    slug = f'btc-updown-5m-{slot}'
    response = requests.get(GAMMA, params={'slug': slug}, timeout=10)
    response.raise_for_status()
    events = response.json()
    if not events or not events[0].get('markets'):
        return None
    market = dict(events[0]['markets'][0])
    market['_event_slug'] = slug
    return market


def market_fields(market: dict[str, Any]) -> dict[str, Any]:
    outcomes = parse_field(market.get('outcomes')) or []
    tokens = parse_field(market.get('clobTokenIds')) or []
    prices = parse_field(market.get('outcomePrices')) or []
    if len(tokens) < 2 or len(prices) < 2:
        raise RuntimeError('market is missing token IDs or outcome prices')
    labels = [str(label).upper() for label in outcomes[:2]]
    up_index = next((i for i, label in enumerate(labels) if 'UP' in label or 'YES' in label), 0)
    down_index = 1 - up_index
    end_iso = str(market.get('endDate') or market.get('endDateIso') or '')
    end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
    return {
        'slug': str(market.get('slug') or market['_event_slug']), 'end_iso': end_iso, 'end_ts': end_ts,
        'UP': {'token': str(tokens[up_index]), 'gamma_price': float(prices[up_index])},
        'DOWN': {'token': str(tokens[down_index]), 'gamma_price': float(prices[down_index])},
    }


def fetch_book(token: str) -> dict[str, Optional[float]]:
    response = requests.get(CLOB, params={'token_id': token}, timeout=8)
    response.raise_for_status()
    book = response.json()
    bids, asks = book.get('bids') or [], book.get('asks') or []
    bid = max((float(level['price']) for level in bids), default=None)
    ask = min((float(level['price']) for level in asks), default=None)
    top_notional = sum(float(level['price']) * float(level['size']) for level in asks
                       if ask is not None and abs(float(level['price']) - ask) < 1e-9)
    return {'bid': bid, 'ask': ask, 'spread': ask - bid if ask is not None and bid is not None else None,
            'top_ask_notional_usd': top_notional}


def fetch_bybit_chart(slot: int) -> dict[str, Any]:
    response = requests.get(BYBIT, params={'category': 'spot', 'symbol': 'BTCUSDT', 'interval': '1',
                                          'start': slot * 1000, 'limit': 6}, timeout=8)
    response.raise_for_status()
    payload = response.json()
    if payload.get('retCode') != 0:
        raise RuntimeError(payload.get('retMsg') or 'Bybit returned an error')
    rows = sorted(payload.get('result', {}).get('list') or [], key=lambda row: int(row[0]))
    rows = [row for row in rows if int(row[0]) >= slot * 1000]
    if not rows:
        raise RuntimeError('Bybit returned no BTC candles')
    prices = [float(row[4]) for row in rows]
    return {'source': 'bybit_public', 'open': float(rows[0][1]), 'last': prices[-1],
            'move_usd': prices[-1] - float(rows[0][1]), 'closes': prices}


def sparkline(values: list[float]) -> str:
    bars = '▁▂▃▄▅▆▇█'
    if not values:
        return ''
    low, high = min(values), max(values)
    if high == low:
        return bars[3] * len(values)
    return ''.join(bars[min(7, int((value - low) / (high - low) * 7))] for value in values)


def analyze(books: dict[str, dict[str, Optional[float]]], move: float, seconds_left: float,
            confirmations: list[str], args) -> dict[str, Any]:
    side = 'UP' if move > 0 else 'DOWN' if move < 0 else None
    selected = books.get(side or '', {})
    other = books.get('DOWN' if side == 'UP' else 'UP', {}) if side else {}
    ask, other_ask = selected.get('ask'), other.get('ask')
    dominance = ask - other_ask if ask is not None and other_ask is not None else None
    checks = {
        'btc_move': args.min_btc_move <= abs(move) <= args.max_btc_move,
        'entry_window': args.entry_min <= seconds_left <= args.entry_max,
        'price': ask is not None and args.threshold <= ask <= args.max_entry_price,
        'direction_agreement': dominance is not None and dominance >= args.min_dominance,
        'spread': selected.get('spread') is not None and selected['spread'] <= args.max_spread,
        'liquidity': selected.get('top_ask_notional_usd') is not None and
                     selected['top_ask_notional_usd'] >= args.min_liquidity,
        'persistence': side is not None and len(confirmations) >= args.confirmation_polls and
                       all(item == side for item in confirmations[-args.confirmation_polls:]),
    }
    return {'full_confidence': all(checks.values()), 'side': side, 'entry_price': ask,
            'checks': checks, 'failed_checks': [key for key, value in checks.items() if not value],
            'confidence': round(sum(checks.values()) / len(checks), 3), 'dominance': dominance,
            'spread': selected.get('spread'), 'liquidity': selected.get('top_ask_notional_usd')}


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token, self.chat_id, self.dry_run = token, chat_id, dry_run

    def send(self, text: str) -> None:
        if self.dry_run:
            print(text, flush=True)
            return
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                                     json={'chat_id': self.chat_id, 'text': text}, timeout=12)
            if response.status_code != 200 or not response.json().get('ok'):
                raise RuntimeError('rejected')
        except Exception as exc:
            raise RuntimeError('Telegram notification failed; verify token, chat ID, and connectivity') from exc


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        state = {}
    day = now_utc().date().isoformat()
    if state.get('utc_day') != day:
        state = {'utc_day': day, 'paper_trades': 0, 'completed': [], 'open': state.get('open')}
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def settlement_side(market: dict[str, Any]) -> Optional[str]:
    fields = market_fields(market)
    up, down = fields['UP']['gamma_price'], fields['DOWN']['gamma_price']
    if up >= .99 and down <= .01:
        return 'UP'
    if down >= .99 and up <= .01:
        return 'DOWN'
    return None


def entry_message(position: dict[str, Any], chart: dict[str, Any], analysis: dict[str, Any]) -> str:
    return (f"🟡 PAPER ENTRY (no real money)\n"
            f"Market: {position['slug']}\nSide: {position['side']}\nPaper stake: $1.00\n"
            f"Entry ask: {position['entry_price']:.3f}\nBTC move: {chart['move_usd']:+.2f} USD\n"
            f"Bybit 1m: {sparkline(chart['closes'])}  {chart['last']:.2f}\n"
            f"Confidence gates: {sum(analysis['checks'].values())}/{len(analysis['checks'])}\n"
            f"Spread: {analysis['spread']:.3f} | liquidity: ${analysis['liquidity']:.2f}")


def result_message(position: dict[str, Any], winner: str) -> str:
    won = position['side'] == winner
    pnl = (1 / position['entry_price'] - 1) if won else -1.0
    status = '🟢 WIN' if won else '🔴 LOSS'
    return (f"{status} — PAPER RESULT\n"
            f"Market: {position['slug']}\nPaper side: {position['side']} | Winner: {winner}\n"
            f"Paper stake: $1.00 | Simulated P/L: {pnl:+.4f} USD\nNo real order was placed.")


def heartbeat_message(fields: dict[str, Any], books: dict[str, dict[str, Optional[float]]],
                      chart: dict[str, Any], state: dict[str, Any]) -> str:
    now = now_utc()
    seconds_left = max(0, int(fields['end_ts'] - time.time()))
    position = state.get('open')
    position_text = (f"{position['side']} @ {position['entry_price']:.3f}" if position else 'none')
    return (f"📊 PAPER BOT STATUS\n"
            f"Time MSK: {now.astimezone(MOSCOW):%Y-%m-%d %H:%M:%S}\n"
            f"Time UTC: {now:%Y-%m-%d %H:%M:%S}\n"
            f"BTC Bybit: ${chart['last']:,.2f}\n"
            f"5m move: {chart['move_usd']:+.2f} USD  {sparkline(chart['closes'])}\n"
            f"Polymarket: {fields['slug']}\n"
            f"UP ask: {books['UP']['ask']} | DOWN ask: {books['DOWN']['ask']}\n"
            f"Seconds left: {seconds_left}\n"
            f"Paper trades today: {state['paper_trades']}/10\n"
            f"Open paper position: {position_text}\n"
            f"Mode: notifications only — real trading disabled")


def main() -> int:
    parser = argparse.ArgumentParser(description='Notification-only BTC 5m paper trader; cannot place orders')
    parser.add_argument('--telegram-token', default=os.getenv('TELEGRAM_BOT_TOKEN', ''))
    parser.add_argument('--telegram-chat-id', default=os.getenv('TELEGRAM_CHAT_ID', ''))
    parser.add_argument('--dry-run', action='store_true', help='Print notifications instead of contacting Telegram')
    parser.add_argument('--poll-sec', type=float, default=3)
    parser.add_argument('--state-file', default=str(Path(__file__).resolve().parents[1] / 'runtime' / 'paper_state.json'))
    parser.add_argument('--max-paper-trades', type=int, default=10)
    parser.add_argument('--threshold', type=float, default=.78)
    parser.add_argument('--max-entry-price', type=float, default=.90)
    parser.add_argument('--min-btc-move', type=float, default=70)
    parser.add_argument('--max-btc-move', type=float, default=400)
    parser.add_argument('--entry-min', type=float, default=90)
    parser.add_argument('--entry-max', type=float, default=150)
    parser.add_argument('--min-dominance', type=float, default=.40)
    parser.add_argument('--max-spread', type=float, default=.03)
    parser.add_argument('--min-liquidity', type=float, default=30)
    parser.add_argument('--confirmation-polls', type=int, default=3)
    args = parser.parse_args()
    if not args.dry_run and (not args.telegram_token or not args.telegram_chat_id):
        parser.error('set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use --dry-run')

    telegram = Telegram(args.telegram_token, args.telegram_chat_id, args.dry_run)
    state_path = Path(args.state_file)
    confirmations: list[str] = []
    last_error = ''
    last_heartbeat_at = 0.0
    telegram.send('🧪 BTC 5m paper notifier started. It cannot place real orders.')

    while True:
        try:
            state = load_state(state_path)
            if time.time() - last_heartbeat_at >= 300:
                status_market = fetch_market()
                if status_market:
                    status_fields = market_fields(status_market)
                    status_slot = int(status_fields['slug'].rsplit('-', 1)[-1])
                    status_books = {'UP': fetch_book(status_fields['UP']['token']),
                                    'DOWN': fetch_book(status_fields['DOWN']['token'])}
                    status_chart = fetch_bybit_chart(status_slot)
                    telegram.send(heartbeat_message(status_fields, status_books, status_chart, state))
                    last_heartbeat_at = time.time()
            position = state.get('open')
            if position:
                if not position.get('entry_notified', False):
                    telegram.send(position['entry_message'])
                    position['entry_notified'] = True
                    save_state(state_path, state)
                market = fetch_market(int(position['slot']))
                winner = settlement_side(market) if market else None
                if winner:
                    telegram.send(result_message(position, winner))
                    position['winner'], position['won'], position['resolved_at'] = winner, position['side'] == winner, now_utc().isoformat()
                    state['completed'].append(position)
                    state['open'] = None
                    save_state(state_path, state)
                time.sleep(args.poll_sec)
                continue

            market = fetch_market()
            if not market:
                time.sleep(args.poll_sec)
                continue
            fields = market_fields(market)
            slot = int(fields['slug'].rsplit('-', 1)[-1])
            if fields['slug'] in {trade['slug'] for trade in state['completed']} or state['paper_trades'] >= args.max_paper_trades:
                time.sleep(args.poll_sec)
                continue
            seconds_left = fields['end_ts'] - time.time()
            books = {'UP': fetch_book(fields['UP']['token']), 'DOWN': fetch_book(fields['DOWN']['token'])}
            chart = fetch_bybit_chart(slot)
            direction = 'UP' if chart['move_usd'] > 0 else 'DOWN' if chart['move_usd'] < 0 else ''
            confirmations.append(direction)
            confirmations = confirmations[-args.confirmation_polls:]
            analysis = analyze(books, chart['move_usd'], seconds_left, confirmations, args)
            if analysis['full_confidence']:
                position = {'slot': slot, 'slug': fields['slug'], 'side': analysis['side'],
                            'entry_price': analysis['entry_price'], 'paper_stake': 1.0,
                            'opened_at': now_utc().isoformat(), 'btc_move': chart['move_usd'],
                            'entry_notified': False}
                position['entry_message'] = entry_message(position, chart, analysis)
                state['open'] = position
                state['paper_trades'] += 1
                save_state(state_path, state)
                telegram.send(position['entry_message'])
                position['entry_notified'] = True
                save_state(state_path, state)
            last_error = ''
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            if error != last_error:
                print(f'paper notifier warning: {error}', flush=True)
                last_error = error
        time.sleep(args.poll_sec)


if __name__ == '__main__':
    raise SystemExit(main())
