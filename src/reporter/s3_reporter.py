from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional


DEFAULT_PREFIX = "reports/paper_performance"


def _number(value: Any, *, digits: int = 2) -> float:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("report values must be numeric") from exc
    if not number.is_finite():
        raise ValueError("report values must be finite")
    quantum = Decimal(1).scaleb(-digits)
    return float(number.quantize(quantum, rounding=ROUND_HALF_UP))


def build_paper_performance_report(
    *,
    proposal: Mapping[str, Any],
    order_result: Mapping[str, Any],
    portfolio_state: Mapping[str, Any],
    risk_state: Mapping[str, Any],
    committed_at: str,
) -> dict[str, Any]:
    """Build a truthful summary from the exact committed paper fill.

    Historical win-rate and trade-count estimates are intentionally omitted:
    the current store API does not expose enough history to calculate them.
    """
    starting_raw = Decimal(str(portfolio_state["starting_cash"]))
    equity_raw = Decimal(str(portfolio_state["total_equity"]))
    starting_cash = _number(starting_raw)
    total_equity = _number(equity_raw)
    total_pnl = _number(equity_raw - starting_raw)
    total_return = _number(((equity_raw - starting_raw) / starting_raw) * 100, digits=6) if starting_raw else 0.0

    symbol = proposal.get("symbol")
    symbol = getattr(symbol, "value", symbol)

    return {
        "schema_version": 1,
        "mode": "PAPER",
        "symbol": str(symbol),
        "strategy_id": str(proposal["strategy_id"]),
        "updated_at": str(committed_at),
        "last_trade": {
            "paper_order_id": str(order_result["paper_order_id"]),
            "fill_id": str(order_result["fill_id"]),
            "action": str(proposal["action"]),
            "quantity_btc": _number(order_result["filled_quantity"], digits=8),
            "price_usd": _number(order_result["filled_price"]),
            "fees_usd": _number(order_result["fees"]),
            "slippage_usd": _number(order_result["slippage"]),
        },
        "performance": {
            "total_pnl_usd": total_pnl,
            "total_return_pct": total_return,
            "realized_pnl_usd": _number(portfolio_state.get("realized_pnl", 0.0)),
            "unrealized_pnl_usd": _number(portfolio_state.get("unrealized_pnl", 0.0)),
            "current_drawdown_pct": _number(float(risk_state.get("current_drawdown", 0.0)) * 100.0, digits=6),
        },
        "portfolio": {
            "starting_cash_usd": starting_cash,
            "current_cash_usd": _number(portfolio_state["available_cash"]),
            "current_btc": _number(portfolio_state.get("btc_quantity", 0.0), digits=8),
            "btc_value_usd": _number(portfolio_state.get("btc_value", 0.0)),
            "average_entry_price_usd": _number(portfolio_state.get("avg_entry_price", 0.0)),
            "total_equity_usd": total_equity,
        },
    }


def deterministic_json(report: Mapping[str, Any]) -> bytes:
    return json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class S3PaperPerformanceReporter:
    """Best-effort writer for the latest paper-performance report."""

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        *,
        client: Any = None,
    ) -> None:
        self.bucket = bucket if bucket is not None else os.environ.get("REPORT_S3_BUCKET", "")
        self.prefix = prefix or os.environ.get("REPORT_S3_PREFIX", DEFAULT_PREFIX)
        self._client = client

    def _client_or_none(self) -> Any:
        if not self.bucket:
            return None
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                config=Config(
                    connect_timeout=1,
                    read_timeout=2,
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
        except Exception:
            return None
        return self._client

    def write_report(self, report: Mapping[str, Any]) -> bool:
        client = self._client_or_none()
        if client is None:
            return False
        try:
            client.put_object(
                Bucket=self.bucket,
                Key=f"{self.prefix.strip('/')}/latest.json",
                Body=deterministic_json(report),
                ContentType="application/json",
            )
        except Exception:
            return False
        return True


def write_report_best_effort(report: Mapping[str, Any]) -> bool:
    return S3PaperPerformanceReporter().write_report(report)
