from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PublicModel(BaseModel):
    """Strict base model for browser-safe reporting data."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value


class Candle(PublicModel):
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    _timestamp_is_aware = field_validator("timestamp")(_require_timezone)

    @model_validator(mode="after")
    def validate_range(self) -> "Candle":
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("candle values must be finite")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC range")
        return self


class PublicTrade(PublicModel):
    executed_at: datetime
    side: Literal["BUY", "SELL"]
    reason: Literal[
        "DIP_ENTRY",
        "TAKE_PROFIT",
        "STOP_LOSS",
        "TRAILING_STOP",
        "PAPER_REBALANCE",
    ]
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    notional: float = Field(gt=0)
    realized_pnl: float | None = None

    _timestamp_is_aware = field_validator("executed_at")(_require_timezone)

    @field_validator("price", "quantity", "notional", "realized_pnl")
    @classmethod
    def values_are_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("trade values must be finite")
        return value


class Portfolio(PublicModel):
    starting_cash: float = Field(ge=0)
    available_cash: float = Field(ge=0)
    btc_quantity: float = Field(ge=0)
    avg_entry_price: float = Field(ge=0)
    current_price: float = Field(gt=0)
    total_equity: float = Field(ge=0)
    realized_pnl: float
    unrealized_pnl: float

    @field_validator("*")
    @classmethod
    def values_are_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("portfolio values must be finite")
        return value


class Position(PublicModel):
    status: Literal["FLAT", "OPEN"]
    quantity: float = Field(ge=0)
    entry_price: float = Field(ge=0)
    current_price: float = Field(gt=0)
    unrealized_pnl: float
    take_profit_price: float | None = Field(default=None, gt=0)
    stop_loss_price: float | None = Field(default=None, gt=0)
    trailing_stop_price: float | None = Field(default=None, gt=0)

    @field_validator("*")
    @classmethod
    def numeric_values_are_finite(cls, value: object) -> object:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("position values must be finite")
        return value

    @model_validator(mode="after")
    def validate_flat_position(self) -> "Position":
        if self.status == "FLAT" and self.quantity != 0:
            raise ValueError("flat positions must have zero quantity")
        return self


class Strategy(PublicModel):
    name: Literal["Deterministic dip entry"]
    status: Literal["ENABLED", "DISABLED"]
    scheduler_status: Literal["ENABLED", "DISABLED", "UNKNOWN"]
    evaluation_frequency: Literal["Every 5 minutes"]
    signal: Literal["BUY", "SELL", "HOLD", "NO_TRADE"]
    last_result: Literal[
        "NO_DIP",
        "SIGNAL_ALREADY_ACTIVE",
        "SIGNAL_REPLAY",
        "COOLDOWN_ACTIVE",
        "PAPER_BUY_COMMITTED",
        "PAPER_EXIT_COMMITTED",
        "EXIT_REJECTED",
        "REJECTED",
        "MARKET_UNAVAILABLE",
        "STATE_UNAVAILABLE",
        "STATE_INVALID",
        "DISABLED",
    ]
    latest_decision: Literal[
        "Waiting for next scheduled evaluation.",
        "No entry signal; monitoring BTC-USD.",
        "Position open; monitoring paper exit thresholds.",
        "Paper entry signal rejected by risk controls.",
        "Automatic paper exit condition met.",
        "Paper entry completed; monitoring paper position.",
        "Automatic paper exit completed.",
        "Evaluation unavailable; previous paper state preserved.",
        "Strategy paused.",
    ]
    last_evaluated_at: datetime
    automatic_exit_status: Literal["IDLE", "ARMED", "TRIGGERED"]
    reference_price: float = Field(gt=0)
    measured_dip_pct: float = Field(ge=0, le=1)
    threshold_pct: float = Field(gt=0, le=1)
    lookback_minutes: Literal[60]
    order_size_usd: float = Field(gt=0)
    cooldown_minutes: Literal[60]
    decision_source: Literal["DETERMINISTIC_RULE"]

    _timestamp_is_aware = field_validator("last_evaluated_at")(_require_timezone)

    @field_validator(
        "reference_price",
        "measured_dip_pct",
        "threshold_pct",
        "order_size_usd",
    )
    @classmethod
    def strategy_values_are_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("strategy values must be finite")
        return value


class RiskStatus(PublicModel):
    status: Literal["NORMAL", "CAUTION", "HALTED"]
    max_position_usd: float = Field(ge=0)
    daily_loss_limit_pct: float = Field(ge=0, le=1)
    max_drawdown_limit_pct: float = Field(ge=0, le=1)
    current_drawdown_pct: float = Field(ge=0, le=1)
    controls_triggered: list[
        Literal[
            "MAX_POSITION",
            "DAILY_LOSS_LIMIT",
            "MAX_DRAWDOWN_LIMIT",
            "STALE_MARKET_DATA",
        ]
    ] = Field(default_factory=list, max_length=4)


class Performance(PublicModel):
    completed_trades: int | None = Field(default=None, ge=0)
    wins: int | None = Field(default=None, ge=0)
    losses: int | None = Field(default=None, ge=0)
    win_rate: float | None = Field(default=None, ge=0, le=1)
    return_pct: float
    max_drawdown_pct: float = Field(ge=0, le=1)

    @field_validator("return_pct")
    @classmethod
    def return_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("return must be finite")
        return value

    @model_validator(mode="after")
    def totals_are_consistent(self) -> "Performance":
        if (
            self.wins is not None
            and self.losses is not None
            and self.completed_trades is not None
            and self.wins + self.losses > self.completed_trades
        ):
            raise ValueError("win/loss totals exceed completed trades")
        return self


class PublicPaperReport(PublicModel):
    schema_version: Literal[1]
    data_status: Literal["LIVE", "SAMPLE"]
    mode: Literal["PAPER"]
    symbol: Literal["BTC-USD"]
    updated_at: datetime
    agent_status: Literal["RUNNING", "DEGRADED", "PAUSED"]
    portfolio: Portfolio
    position: Position
    strategy: Strategy
    risk: RiskStatus
    performance: Performance
    trades: list[PublicTrade] = Field(default_factory=list, max_length=500)
    candles: list[Candle] = Field(default_factory=list, max_length=5000)

    _timestamp_is_aware = field_validator("updated_at")(_require_timezone)

    @model_validator(mode="after")
    def data_is_chronological(self) -> "PublicPaperReport":
        if self.candles != sorted(self.candles, key=lambda candle: candle.timestamp):
            raise ValueError("candles must be chronological")
        if self.trades != sorted(self.trades, key=lambda trade: trade.executed_at):
            raise ValueError("trades must be chronological")
        return self
