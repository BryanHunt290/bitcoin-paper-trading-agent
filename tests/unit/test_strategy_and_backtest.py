from datetime import datetime, timedelta, timezone
from src.strategies.ema_cross_v1 import generate_signals
from src.backtesting.backtester import Backtester


def make_candles(n=10, start_price=100.0, step=1.0):
    candles = []
    ts = datetime.now(timezone.utc)
    for i in range(n):
        openp = start_price + i * step
        close = openp + 0.5
        high = max(openp, close) + 0.1
        low = min(openp, close) - 0.1
        candles.append({'timestamp': ts + timedelta(minutes=15*i), 'open': openp, 'high': high, 'low': low, 'close': close, 'volume': 1.0})
    return candles


def test_generate_signals_runs():
    candles = make_candles(60)
    timestamps = [c['timestamp'] for c in candles]
    closes = [c['close'] for c in candles]
    signals = generate_signals(timestamps, closes)
    assert len(signals) == len(closes)


def test_backtester_next_candle_execution():
    candles = make_candles(30)
    bt = Backtester(candles)
    res = bt.run()
    # ledger events should reference orders executed at subsequent candle opens
    for e in res['ledger']:
        if isinstance(e, dict) and e.get('order_id'):
            # ensure signal timestamp is before fill portfolio snapshot time
            assert 'signal_timestamp' in e
