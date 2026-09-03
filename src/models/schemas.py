from __future__ import annotations
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime


class OHLCV(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketDataResponse(BaseModel):
    symbol: Literal['BTC-USD']
    timeframe: str
    timestamps: List[datetime]
    ohlcv: List[OHLCV]
    latest_price: float
    source: str
    retrieved_at: datetime


class IndicatorSet(BaseModel):
    ema20: List[Optional[float]]
    ema50: List[Optional[float]]
    rsi14: List[Optional[float]]
    atr14: List[Optional[float]]


class AgentDecision(BaseModel):
    symbol: Literal['BTC-USD']
    action: Literal['BUY', 'SELL', 'HOLD', 'NO_TRADE']
    strategy_id: str
    market_regime: Optional[str]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str
    evidence: List[Dict[str, Any]] = []
    requested_notional_usd: float = 0.0
    stop_price: Optional[float] = None
    risk_tool_required: bool = True
    timestamp: datetime


class RiskDecision(BaseModel):
    allowed: bool
    reason_code: Optional[str]
    max_notional: float = 0.0
    position_size_btc: float = 0.0
    equity: float = 0.0
    details: Dict[str, Any] = {}


class PaperOrderResult(BaseModel):
    paper_order_id: str
    fill_id: Optional[str]
    filled_quantity: float
    filled_price: float
    fees: float
    slippage: float
    portfolio_state: Dict[str, Any]
