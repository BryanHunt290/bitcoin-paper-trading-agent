from typing import Dict, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Portfolio:
    starting_cash: float = 10000.0
    cash: float = 10000.0
    btc_quantity: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_snapshot(cls, snapshot: Dict[str, Any]) -> "Portfolio":
        """Rebuild authoritative paper state from a persisted portfolio snapshot."""
        if not isinstance(snapshot, dict):
            raise ValueError('portfolio snapshot must be an object')
        updated = snapshot.get('last_updated')
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated.replace('Z', '+00:00'))
        if not isinstance(updated, datetime) or updated.tzinfo is None:
            raise ValueError('portfolio snapshot last_updated must be timezone-aware')
        portfolio = cls(
            starting_cash=float(snapshot['starting_cash']),
            cash=float(snapshot['available_cash']),
            btc_quantity=float(snapshot.get('btc_quantity', 0.0)),
            avg_entry_price=float(snapshot.get('avg_entry_price', 0.0)),
            realized_pnl=float(snapshot.get('realized_pnl', 0.0)),
            last_updated=updated,
        )
        if portfolio.cash < 0 or portfolio.btc_quantity < 0:
            raise ValueError('portfolio snapshot contains negative exposure or cash')
        return portfolio

    def snapshot(self, market_price: float) -> Dict[str, Any]:
        btc_value = self.btc_quantity * market_price
        total_equity = self.cash + btc_value
        unrealized = (market_price - self.avg_entry_price) * self.btc_quantity if self.btc_quantity > 0 else 0.0
        return {
            'starting_cash': self.starting_cash,
            'available_cash': self.cash,
            'btc_quantity': self.btc_quantity,
            'btc_value': btc_value,
            'avg_entry_price': self.avg_entry_price,
            'realized_pnl': self.realized_pnl,
            'unrealized_pnl': unrealized,
            'total_equity': total_equity,
            'last_updated': self.last_updated.isoformat(),
        }

    def apply_fill(self, side: str, quantity: float, price: float, fees: float = 0.0):
        # Conservative: BUY increases btc and decreases cash
        if side == 'BUY':
            cost = quantity * price + fees
            self.cash -= cost
            # update average entry price
            if self.btc_quantity + quantity > 0:
                prev_value = self.btc_quantity * self.avg_entry_price
                new_total = prev_value + quantity * price
                self.btc_quantity += quantity
                self.avg_entry_price = new_total / self.btc_quantity
            else:
                self.btc_quantity += quantity
                self.avg_entry_price = price
        elif side == 'SELL':
            proceeds = quantity * price - fees
            self.cash += proceeds
            # reduce quantity and compute realized pnl
            if quantity > self.btc_quantity:
                # sell more than holdings not allowed in spot-only paper broker
                raise ValueError('Attempt to sell more BTC than held')
            realized = quantity * (price - self.avg_entry_price)
            self.realized_pnl += realized
            self.btc_quantity -= quantity
            if self.btc_quantity == 0:
                self.avg_entry_price = 0.0
        else:
            raise ValueError('Unknown side')
        self.last_updated = datetime.now(timezone.utc)
