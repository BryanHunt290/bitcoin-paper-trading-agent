from typing import Dict, Any
from dataclasses import dataclass


@dataclass
class RiskConfig:
    starting_capital: float = 10000.0
    max_risk_per_trade_pct: float = 0.005  # 0.5%
    max_btc_exposure_pct: float = 0.20
    max_concurrent_positions: int = 1
    leverage: int = 0
    allow_short: bool = False
    daily_loss_cutoff: float = 0.10  # 10% daily loss
    max_drawdown_cutoff: float = 0.30  # 30% drawdown
    execution_cost_buffer_pct: float = 0.002  # covers paper slippage and fees


def calculate_position_size(requested_notional_usd: float, portfolio_state: Dict[str, Any], config: RiskConfig = RiskConfig(), persisted_state: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Deterministic position sizing and validation.

    Returns dict with allowed(bool), reason_code(str|None), max_notional(float), position_size_btc(float)
    """
    # Only BTC-USD allowed — enforced at higher layer
    equity = portfolio_state.get('total_equity', config.starting_capital)
    # If persisted risk state is provided, derive peak and day baseline from it
    peak_equity = None
    day_start_equity = None
    if persisted_state:
        peak_equity = persisted_state.get('peak_equity')
        day_start_equity = persisted_state.get('day_start_equity')
    btc_value = portfolio_state.get('btc_value', 0.0)
    current_btc_qty = portfolio_state.get('btc_quantity', 0.0)
    open_positions = 1 if current_btc_qty > 0 else 0

    max_notional_by_risk = equity * config.max_risk_per_trade_pct
    max_notional_by_exposure = equity * config.max_btc_exposure_pct - btc_value
    if max_notional_by_exposure < 0:
        max_notional_by_exposure = 0.0
    available_cash = max(0.0, float(portfolio_state.get('available_cash', 0.0)))
    max_notional_by_cash = available_cash / (1.0 + config.execution_cost_buffer_pct)
    max_allowed = min(max_notional_by_risk, max_notional_by_exposure, max_notional_by_cash)

    # enforce daily loss and drawdown conservative cutoffs if available
    equity = portfolio_state.get('total_equity', config.starting_capital)
    # daily loss cutoff using day_start_equity when available
    if day_start_equity is not None:
        if equity <= day_start_equity * (1.0 - config.daily_loss_cutoff):
            return {'allowed': False, 'reason_code': 'DAILY_LOSS_CUTOFF', 'max_notional': 0.0, 'position_size_btc': 0.0, 'equity': equity}
    else:
        # fallback conservative check against starting capital
        if equity <= config.starting_capital * (1.0 - config.daily_loss_cutoff):
            return {'allowed': False, 'reason_code': 'DAILY_LOSS_CUTOFF', 'max_notional': 0.0, 'position_size_btc': 0.0, 'equity': equity}

    # drawdown cutoff using peak_equity when available
    if peak_equity is not None and peak_equity > 0:
        drawdown = (peak_equity - equity) / peak_equity
        if drawdown >= config.max_drawdown_cutoff:
            return {'allowed': False, 'reason_code': 'MAX_DRAWDOWN_CUTOFF', 'max_notional': 0.0, 'position_size_btc': 0.0, 'equity': equity}
    else:
        if equity <= config.starting_capital * (1.0 - config.max_drawdown_cutoff):
            return {'allowed': False, 'reason_code': 'MAX_DRAWDOWN_CUTOFF', 'max_notional': 0.0, 'position_size_btc': 0.0, 'equity': equity}

    if requested_notional_usd <= 0:
        return {'allowed': False, 'reason_code': 'INVALID_NOTIONAL', 'max_notional': 0.0, 'position_size_btc': 0.0, 'equity': equity}
    if open_positions >= config.max_concurrent_positions:
        return {'allowed': False, 'reason_code': 'MAX_POSITIONS_EXCEEDED', 'max_notional': max_allowed, 'position_size_btc': 0.0, 'equity': equity}
    if requested_notional_usd > max_notional_by_cash:
        return {'allowed': False, 'reason_code': 'INSUFFICIENT_PAPER_BALANCE', 'max_notional': max_notional_by_cash, 'position_size_btc': 0.0, 'equity': equity}
    if requested_notional_usd > max_allowed:
        return {'allowed': False, 'reason_code': 'NOTIONAL_TOO_LARGE', 'max_notional': max_allowed, 'position_size_btc': 0.0, 'equity': equity}

    # compute position size in BTC using current price
    price = portfolio_state.get('latest_price') or portfolio_state.get('market_price')
    if not price or price <= 0:
        return {'allowed': False, 'reason_code': 'INVALID_PRICE', 'max_notional': max_allowed, 'position_size_btc': 0.0, 'equity': equity}
    position_btc = requested_notional_usd / price
    return {'allowed': True, 'reason_code': None, 'max_notional': max_allowed, 'position_size_btc': position_btc, 'equity': equity}
