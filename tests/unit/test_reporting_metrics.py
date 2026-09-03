import json
from datetime import datetime, timezone

from src.agent.models import AgentTradeProposal, MarketDataSnapshot, PaperOrderRequest
from src.agent.store import InMemoryTradeEventStore
from src.agent.tools import AgentTradeService
from src.broker.paper_broker import PaperBroker
from src.portfolio.portfolio import Portfolio
from src.reporter.s3_reporter import (
    S3PaperPerformanceReporter,
    build_paper_performance_report,
    deterministic_json,
)


class RecordingS3:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def put_object(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)


def sample_report():
    return build_paper_performance_report(
        proposal={"symbol": "BTC-USD", "action": "SELL", "strategy_id": "dip_buy_v1"},
        order_result={
            "paper_order_id": "ORDER#1",
            "fill_id": "FILL#1",
            "filled_quantity": 0.123456789,
            "filled_price": 20_010.129,
            "fees": 2.345,
            "slippage": 1.234,
        },
        portfolio_state={
            "starting_cash": 10_000,
            "available_cash": 10_012.345,
            "btc_quantity": 0,
            "btc_value": 0,
            "avg_entry_price": 0,
            "realized_pnl": 12.345,
            "unrealized_pnl": 0,
            "total_equity": 10_012.345,
        },
        risk_state={"current_drawdown": 0.001234567},
        committed_at="2026-09-02T12:00:00+00:00",
    )


def make_request(portfolio):
    now = datetime.now(timezone.utc)
    proposal = AgentTradeProposal(
        symbol="BTC-USD",
        action="BUY",
        requested_notional_usd=10.0,
        timestamp=now,
        idempotency_key="report-test-1",
        strategy_id="ema_cross_v1",
        execution_mode="PAPER",
    )
    market = MarketDataSnapshot(symbol="BTC-USD", price=20_000.0, timestamp=now)
    return PaperOrderRequest(
        proposal=proposal,
        portfolio_snapshot=portfolio.snapshot(market.price),
        market_snapshot=market,
    )


def test_report_body_and_s3_key_are_deterministic():
    client = RecordingS3()
    reporter = S3PaperPerformanceReporter(bucket="paper-reports", client=client)
    report = sample_report()

    assert reporter.write_report(report) is True
    assert reporter.write_report(report) is True
    assert client.calls[0]["Key"] == "reports/paper_performance/latest.json"
    assert client.calls[0]["ContentType"] == "application/json"
    assert client.calls[0]["Body"] == client.calls[1]["Body"] == deterministic_json(report)
    assert json.loads(client.calls[0]["Body"])["performance"] == {
        "current_drawdown_pct": 0.123457,
        "realized_pnl_usd": 12.35,
        "total_pnl_usd": 12.35,
        "total_return_pct": 0.12345,
        "unrealized_pnl_usd": 0.0,
    }


def test_reporter_is_disabled_without_bucket_and_swallows_s3_errors():
    assert S3PaperPerformanceReporter(bucket="", client=RecordingS3()).write_report(sample_report()) is False
    failing = S3PaperPerformanceReporter(bucket="paper-reports", client=RecordingS3(RuntimeError("down")))
    assert failing.write_report(sample_report()) is False


def test_successful_commit_reports_once_and_idempotent_replay_does_not_repeat():
    reports = []
    portfolio = Portfolio()
    service = AgentTradeService(
        broker=PaperBroker(portfolio),
        store=InMemoryTradeEventStore(),
        report_writer=reports.append,
    )
    request = make_request(portfolio)

    first = service.submit_paper_order(request)
    replay = service.submit_paper_order(request)

    assert replay.fill_id == first.fill_id
    assert len(reports) == 1
    assert reports[0]["mode"] == "PAPER"
    assert reports[0]["symbol"] == "BTC-USD"
    assert reports[0]["last_trade"]["action"] == "BUY"
    assert reports[0]["last_trade"]["fill_id"] == first.fill_id


def test_reporting_failure_cannot_fail_a_committed_paper_trade():
    def fail(_report):
        raise RuntimeError("reporting unavailable")

    portfolio = Portfolio()
    service = AgentTradeService(
        broker=PaperBroker(portfolio),
        store=InMemoryTradeEventStore(),
        report_writer=fail,
    )

    result = service.submit_paper_order(make_request(portfolio))

    assert result.paper_order_id
