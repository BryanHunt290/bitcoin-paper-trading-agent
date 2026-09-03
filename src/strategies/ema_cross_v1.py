from typing import List, Dict, Any, Optional
from datetime import datetime
from ..indicators.indicators import ema


STRATEGY_ID = 'ema_cross_v1'


def generate_signals(timestamps: List[datetime], closes: List[float]) -> List[Dict[str, Any]]:
    """Generate signals based on EMA20/EMA50 crossover.

    Signal at index i is based on data up to i (no look-ahead). Execution must be at i+1.
    Returns list of {'timestamp': timestamps[i], 'signal': 'BUY'|'SELL'|'HOLD'}
    """
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    signals: List[Dict[str, Any]] = []
    for i in range(len(closes)):
        action = 'HOLD'
        if i == 0:
            signals.append({'timestamp': timestamps[i], 'signal': action, 'index': i})
            continue
        prev20 = ema20[i-1]
        prev50 = ema50[i-1]
        cur20 = ema20[i]
        cur50 = ema50[i]
        if prev20 is None or prev50 is None or cur20 is None or cur50 is None:
            action = 'HOLD'
        else:
            # crossover detection
            if prev20 <= prev50 and cur20 > cur50:
                action = 'BUY'
            elif prev20 >= prev50 and cur20 < cur50:
                action = 'SELL'
            else:
                action = 'HOLD'
        signals.append({'timestamp': timestamps[i], 'signal': action, 'index': i})
    return signals
