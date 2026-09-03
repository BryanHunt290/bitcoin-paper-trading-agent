import uuid
import copy
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from ..portfolio.portfolio import Portfolio


class PaperBroker:
    def __init__(self, portfolio: Portfolio, fee_pct: float = 0.001, slippage_pct: float = 0.0005):
        self.portfolio = portfolio
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct

    def _execute_order(self, side: str, quantity: float, price: float, strategy_id: str, signal_ts: datetime, apply_to_portfolio: bool = True) -> Dict[str, Any]:
        """Internal broker execution. This is intentionally private — external callers MUST use the centralized submit_paper_order tool which enforces deterministic risk validation.

        Do NOT call this from agent-facing code.
        """
        if side not in ('BUY', 'SELL'):
            raise ValueError('Invalid side')
        if quantity is None or quantity <= 0:
            raise ValueError('Invalid quantity')

        order_id = f"ORDER#{uuid.uuid4().hex}"
        fill_id = f"FILL#{uuid.uuid4().hex}"
        slippage = price * self.slippage_pct
        executed_price = price + slippage if side == 'BUY' else price - slippage
        fees = executed_price * quantity * self.fee_pct
        if apply_to_portfolio:
            self.portfolio.apply_fill(side, quantity, executed_price, fees)
            portfolio_snapshot = self.portfolio.snapshot(executed_price)
        else:
            # Calculate the post-fill candidate on a copy. Authoritative state is
            # mutated only after the persistence transaction succeeds.
            candidate = copy.deepcopy(self.portfolio)
            candidate.apply_fill(side, quantity, executed_price, fees)
            portfolio_snapshot = candidate.snapshot(executed_price)

        event = {
            'order_id': order_id,
            'fill_id': fill_id,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'side': side,
            'quantity': quantity,
            'executed_price': executed_price,
            'fees': fees,
            'slippage': slippage,
            'portfolio': portfolio_snapshot,
            'strategy_id': strategy_id,
            'signal_timestamp': signal_ts.isoformat(),
        }
        return event

    def submit_order(self, *args, **kwargs):
        raise RuntimeError('submit_order is disabled. Use src.agent.tools.submit_paper_order for deterministic, audited paper trades')
