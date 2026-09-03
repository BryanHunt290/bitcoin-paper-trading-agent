from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from datetime import datetime
from enum import Enum


class Symbol(str, Enum):
    BTC_USD = 'BTC-USD'


class Side(str, Enum):
    BUY = 'BUY'
    SELL = 'SELL'


class ExecutionMode(str, Enum):
    PAPER = 'PAPER'


class AgentTradeProposal(BaseModel):
    symbol: Symbol
    action: Literal['BUY', 'SELL', 'HOLD', 'NO_TRADE']
    strategy_id: str
    requested_notional_usd: Optional[float] = 0.0
    requested_quantity: Optional[float] = None
    stop_price: Optional[float] = None
    timestamp: datetime
    idempotency_key: Optional[str] = None
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    allow_risk_resizing: bool = False

    model_config = {
        'extra': 'forbid'
    }

    @field_validator('requested_notional_usd', mode='before')
    def notional_non_negative(cls, v):
        if v is None:
            return 0.0
        if v < 0:
            raise ValueError('requested_notional_usd must be non-negative')
        return v


class MarketDataSnapshot(BaseModel):
    symbol: Symbol
    price: float
    timestamp: datetime


class RiskRequest(BaseModel):
    portfolio_snapshot: dict
    market_snapshot: MarketDataSnapshot
    requested_notional_usd: float
    requested_quantity: Optional[float]
    action: Literal['BUY', 'SELL']
    strategy_id: str


class RejectionReason(str, Enum):
    INVALID_ASSET = 'INVALID_ASSET'
    LEVERAGE_NOT_ALLOWED = 'LEVERAGE_NOT_ALLOWED'
    SHORTING_NOT_ALLOWED = 'SHORTING_NOT_ALLOWED'
    INVALID_NOTIONAL = 'INVALID_NOTIONAL'
    INVALID_QUANTITY = 'INVALID_QUANTITY'
    STALE_MARKET_DATA = 'STALE_MARKET_DATA'
    MAX_POSITIONS_EXCEEDED = 'MAX_POSITIONS_EXCEEDED'
    NOTIONAL_TOO_LARGE = 'NOTIONAL_TOO_LARGE'
    INSUFFICIENT_PAPER_BALANCE = 'INSUFFICIENT_PAPER_BALANCE'
    DAILY_LOSS_CUTOFF = 'DAILY_LOSS_CUTOFF'
    MAX_DRAWDOWN_CUTOFF = 'MAX_DRAWDOWN_CUTOFF'
    UNSUPPORTED_STRATEGY = 'UNSUPPORTED_STRATEGY'
    PAPER_MODE_REQUIRED = 'PAPER_MODE_REQUIRED'
    OTHER = 'OTHER'


class RiskDecision(BaseModel):
    allowed: bool
    reason_code: Optional[RejectionReason]
    max_notional: float = 0.0
    position_size_btc: float = 0.0
    equity: float = 0.0
    risk_decision_id: Optional[str] = None


class PaperOrderRequest(BaseModel):
    proposal: AgentTradeProposal
    portfolio_snapshot: dict
    market_snapshot: MarketDataSnapshot


class PaperOrderResult(BaseModel):
    paper_order_id: str
    fill_id: str
    filled_quantity: float
    filled_price: float
    fees: float
    slippage: float
    portfolio_state: dict
    risk_decision_id: str


class MarketRegime(str, Enum):
    TRENDING = 'TRENDING'
    SIDEWAYS = 'SIDEWAYS'
    HIGH_VOLATILITY = 'HIGH_VOLATILITY'
    LOW_VOLATILITY = 'LOW_VOLATILITY'
    UNKNOWN = 'UNKNOWN'


class AgentDecision(BaseModel):
    symbol: Symbol
    action: Literal['BUY', 'SELL', 'HOLD', 'NO_TRADE']
    strategy_id: str
    market_regime: MarketRegime = MarketRegime.UNKNOWN
    confidence: float = 0.0
    reasoning_summary: Optional[str] = None
    evidence: Optional[list] = None
    requested_notional_usd: float = 0.0
    stop_price: Optional[float] = None
    idempotency_key: Optional[str] = None
    timestamp: datetime

    @field_validator('confidence')
    def confidence_in_range(cls, v):
        if v is None:
            return 0.0
        if not (0.0 <= v <= 1.0):
            raise ValueError('confidence must be between 0 and 1')
        return v

    @field_validator('requested_notional_usd')
    def notional_valid(cls, v):
        if v is None:
            return 0.0
        if v != v or v == float('inf') or v == float('-inf'):
            raise ValueError('requested_notional_usd must be finite')
        if v < 0:
            raise ValueError('requested_notional_usd must be non-negative')
        return v

    model_config = {
        'extra': 'forbid'
    }


class PortfolioRiskState(BaseModel):
    portfolio_id: str
    current_equity: float
    peak_equity: float
    day_start_equity: float
    trading_day_utc: str  # YYYY-MM-DD
    daily_pnl: float
    current_drawdown: float
    updated_at: datetime
    version: int = 1

    model_config = {
        'extra': 'forbid'
    }

    @field_validator('current_equity', 'peak_equity', 'day_start_equity', 'daily_pnl', 'current_drawdown')
    def finite_non_negative(cls, v):
        if v is None:
            raise ValueError('numeric fields must be provided')
        if v != v or v == float('inf') or v == float('-inf'):
            raise ValueError('numeric fields must be finite')
        return v
