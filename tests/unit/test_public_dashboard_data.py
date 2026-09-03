from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from src.public_dashboard.data_source import (
    PUBLIC_ERROR_MESSAGE,
    PublicReportUnavailable,
    load_public_report,
    parse_public_report,
)


SAMPLE_PATH = Path(__file__).resolve().parents[2] / "data" / "public_report.example.json"


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, content: bytes) -> None:
        self.content = content

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self) -> None:
        pass


def test_public_report_contract_accepts_sanitized_sample():
    report = parse_public_report(SAMPLE_PATH.read_bytes())

    assert report.mode == "PAPER"
    assert report.symbol == "BTC-USD"
    assert report.portfolio.available_cash == pytest.approx(7690.15)
    assert report.position.status == "OPEN"
    assert report.strategy.automatic_exit_status == "ARMED"
    assert report.strategy.scheduler_status == "ENABLED"
    assert report.strategy.last_result == "NO_DIP"
    assert len(report.trades) == 3
    assert len(report.candles) == 20


def test_live_status_contract_accepts_unavailable_history_metrics():
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    payload["data_status"] = "LIVE"
    payload["performance"].update(
        {
            "completed_trades": None,
            "wins": None,
            "losses": None,
            "win_rate": None,
        }
    )
    payload["trades"] = []
    payload["candles"] = []

    report = parse_public_report(json.dumps(payload).encode())

    assert report.data_status == "LIVE"
    assert report.performance.completed_trades is None
    assert report.candles == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "LIVE"),
        ("symbol", "ETH-USD"),
        ("aws_account_id", "not-public"),
        ("lambda_name", "not-public"),
        ("bucket_name", "not-public"),
        ("table_name", "not-public"),
    ],
)
def test_malformed_or_infrastructure_fields_fail_closed(field: str, value: str):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    payload[field] = value

    with pytest.raises(PublicReportUnavailable, match=f"^{PUBLIC_ERROR_MESSAGE}$"):
        parse_public_report(json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("strategy", "latest_decision", "Internal resource identifier"),
        ("risk", "controls_triggered", ["INTERNAL_CONTROL"]),
    ],
)
def test_free_form_internal_text_is_not_accepted(section: str, field: str, value: object):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    payload[section][field] = value

    with pytest.raises(PublicReportUnavailable, match=f"^{PUBLIC_ERROR_MESSAGE}$"):
        parse_public_report(json.dumps(payload).encode())


def test_unavailable_reporting_source_returns_only_safe_error(monkeypatch):
    def unavailable(*args, **kwargs):
        raise requests.Timeout("internal endpoint detail")

    monkeypatch.setattr(requests, "get", unavailable)

    with pytest.raises(PublicReportUnavailable) as exc_info:
        load_public_report(
            "https://reports.example.invalid/public-report.json",
            "reports.example.invalid",
        )

    assert str(exc_info.value) == PUBLIC_ERROR_MESSAGE
    assert "endpoint" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://reports.example.invalid/report.json", "reports.example.invalid"),
        ("https://other.example.invalid/report.json", "reports.example.invalid"),
        (
            "https://" + "user" + ":" + "pass" + "@" + "reports.example.invalid/report.json",
            "reports.example.invalid",
        ),
        ("https://reports.example.invalid/report.json?operation=write", "reports.example.invalid"),
        ("https://reports.example.invalid:invalid/report.json", "reports.example.invalid"),
    ],
)
def test_remote_source_is_fixed_https_read_only(url: str, host: str):
    with pytest.raises(PublicReportUnavailable, match=f"^{PUBLIC_ERROR_MESSAGE}$"):
        load_public_report(url, host)


def test_remote_source_uses_get_without_credentials_or_redirects(monkeypatch):
    observed = {}

    def fake_get(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return FakeResponse(SAMPLE_PATH.read_bytes())

    monkeypatch.setattr(requests, "get", fake_get)
    report = load_public_report(
        "https://reports.example.invalid/public-report.json",
        "reports.example.invalid",
    )

    assert report.mode == "PAPER"
    assert observed["allow_redirects"] is False
    assert observed["stream"] is True
    assert observed["headers"] == {
        "Accept": "application/json",
        "User-Agent": "paper-observability-dashboard/1",
    }
