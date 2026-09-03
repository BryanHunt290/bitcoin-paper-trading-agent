from typing import List, Dict, Any
from datetime import datetime, timezone
import requests
import math
from ..models.schemas import MarketDataResponse, OHLCV


COINBASE_CANDLES = 'https://api.exchange.coinbase.com/products/{symbol}/candles'


def get_market_data(symbol: str, timeframe: str, candle_count: int) -> MarketDataResponse:
    # allowlist
    if symbol != 'BTC-USD':
        raise ValueError('Only BTC-USD is supported')
    if timeframe != '15m':
        raise ValueError('Only the 15m timeframe is supported')
    if not isinstance(candle_count, int) or not (1 <= candle_count <= 300):
        raise ValueError('candle_count must be between 1 and 300')
    # timeframe is for bookkeeping; implement only 15m externally
    params = {'granularity': 900, 'limit': candle_count}
    url = COINBASE_CANDLES.format(symbol=symbol)
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        raise ValueError('Coinbase returned no candle data')
    # Coinbase returns [time, low, high, open, close, volume] per row
    data_sorted = sorted(data, key=lambda r: r[0])
    timestamps = []
    ohlcv = []
    latest_price = None
    for row in data_sorted:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError('Coinbase returned a malformed candle')
        ts = datetime.fromtimestamp(row[0], timezone.utc)
        low, high, openp, closep, vol = (float(row[i]) for i in range(1, 6))
        if not all(math.isfinite(v) for v in (low, high, openp, closep, vol)):
            raise ValueError('Coinbase returned a non-finite candle value')
        if low <= 0 or high <= 0 or openp <= 0 or closep <= 0 or vol < 0:
            raise ValueError('Coinbase returned an invalid candle value')
        if low > min(openp, closep) or high < max(openp, closep) or low > high:
            raise ValueError('Coinbase returned inconsistent OHLC data')
        timestamps.append(ts)
        ohlcv.append(OHLCV(timestamp=ts, open=openp, high=high, low=low, close=closep, volume=vol))
        latest_price = closep
    return MarketDataResponse(symbol='BTC-USD', timeframe=timeframe, timestamps=timestamps, ohlcv=ohlcv, latest_price=latest_price, source='coinbase', retrieved_at=datetime.now(timezone.utc))
