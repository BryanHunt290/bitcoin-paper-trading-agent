from datetime import datetime
from src.indicators.indicators import ema, rsi, atr


def test_ema_simple():
    closes = [i for i in range(1, 51)]
    out = ema(closes, 10)
    # EMA should be None for first few entries, and produce floats later
    assert out[0] is None
    assert out[-1] is not None


def test_rsi_basic():
    closes = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    out = rsi(closes, 14)
    assert out[-1] is not None


def test_atr_basic():
    highs = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
    lows = [0.5,1.5,2.5,3.5,4.5,5.5,6.5,7.5,8.5,9.5,10.5,11.5,12.5,13.5,14.5]
    closes = [0.8,1.8,2.8,3.8,4.8,5.8,6.8,7.8,8.8,9.8,10.8,11.8,12.8,13.8,14.8]
    out = atr(highs, lows, closes, 14)
    assert out[-1] is not None
