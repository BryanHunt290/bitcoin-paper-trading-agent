from typing import List, Dict, Any
from datetime import datetime
from ..strategies.ema_cross_v1 import generate_signals, STRATEGY_ID
from ..broker.paper_broker import PaperBroker
from ..portfolio.portfolio import Portfolio
from ..risk.risk_engine import calculate_position_size


class Backtester:
    def __init__(self, candles: List[Dict[str, Any]], starting_cash: float = 10000.0, fee_pct: float = 0.001, slippage_pct: float = 0.0005):
        self.candles = candles
        self.portfolio = Portfolio(starting_cash, starting_cash)
        self.broker = PaperBroker(self.portfolio, fee_pct=fee_pct, slippage_pct=slippage_pct)

    def run(self) -> Dict[str, Any]:
        timestamps = [c['timestamp'] for c in self.candles]
        closes = [c['close'] for c in self.candles]
        signals = generate_signals(timestamps, closes)
        ledger = []
        # iterate; signals at i execute at i+1 (next candle open)
        for sig in signals:
            idx = sig['index']
            action = sig['signal']
            # cannot execute on last candle
            if idx + 1 >= len(self.candles):
                continue
            exec_price = self.candles[idx+1]['open']
            if action == 'BUY':
                # request max affordable by risk
                portfolio_state = self.portfolio.snapshot(exec_price)
                portfolio_state['latest_price'] = exec_price
                risk = calculate_position_size(1000.0, portfolio_state)
                if not risk.get('allowed'):
                    ledger.append({'index': idx, 'action': 'REJECTED', 'reason': risk.get('reason_code')})
                    continue
                qty = risk['position_size_btc']
                event = self.broker._execute_order('BUY', qty, exec_price, STRATEGY_ID, timestamps[idx], apply_to_portfolio=True)
                ledger.append(event)
            elif action == 'SELL':
                # close entire position conservatively
                if self.portfolio.btc_quantity <= 0:
                    ledger.append({'index': idx, 'action': 'NO_POSITION'})
                    continue
                qty = self.portfolio.btc_quantity
                event = self.broker._execute_order('SELL', qty, exec_price, STRATEGY_ID, timestamps[idx], apply_to_portfolio=True)
                ledger.append(event)
        return {'ledger': ledger, 'final_portfolio': self.portfolio.snapshot(self.candles[-1]['close'])}
