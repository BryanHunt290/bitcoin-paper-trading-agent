from typing import List, Optional
from math import isnan


def ema(values: List[float], period: int) -> List[Optional[float]]:
    if period <= 0:
        raise ValueError('period must be > 0')
    result: List[Optional[float]] = [None] * len(values)
    k = 2 / (period + 1)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if v is None:
            result[i] = None
            continue
        if prev is None:
            # seed with simple average of first period if available
            start = i - period + 1
            if start >= 0:
                window = [x for x in values[start:i+1] if x is not None]
                if len(window) == period:
                    prev = sum(window) / period
                    result[i] = prev
                else:
                    result[i] = None
            else:
                result[i] = None
        else:
            prev = (v - prev) * k + prev
            result[i] = prev
    return result


def rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    result: List[Optional[float]] = [None] * len(closes)
    if period <= 0:
        raise ValueError('period must be > 0')
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = None
    avg_loss = None
    for i in range(len(closes)):
        if i < period:
            result[i] = None
            continue
        if i == period:
            avg_gain = sum(gains[1:period+1]) / period
            avg_loss = sum(losses[1:period+1]) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rs = float('inf')
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100 - (100 / (1 + rs))
        result[i] = rsi_val
    return result


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    tr: List[float] = []
    for i in range(len(highs)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    result: List[Optional[float]] = [None] * len(tr)
    if period <= 0:
        raise ValueError('period must be > 0')
    atr_val = None
    for i in range(len(tr)):
        if i < period:
            result[i] = None
            continue
        if i == period:
            atr_val = sum(tr[1:period+1]) / period
        else:
            atr_val = (atr_val * (period - 1) + tr[i]) / period
        result[i] = atr_val
    return result
