from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[2]
APP_PATH = ROOT / "streamlit_app.py"
PUBLIC_PACKAGE = ROOT / "src" / "public_dashboard"


def run_app() -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=15).run()


def test_dashboard_renders_portfolio_history_and_paper_mode(monkeypatch):
    monkeypatch.delenv("PUBLIC_REPORT_URL", raising=False)
    monkeypatch.delenv("PUBLIC_REPORT_ALLOWED_HOST", raising=False)
    app = run_app()

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["BTC-USD"] == "$76,844.20"
    assert metrics["Paper account equity"] == "$10,010.84"
    assert metrics["Simulated cash"] == "$7,690.15"
    assert metrics["Simulated BTC"] == "0.03020000 BTC"
    assert metrics["Average entry"] == "$75,310.42"
    assert metrics["Realized P&L"] == "$-35.64"
    assert metrics["Unrealized P&L"] == "$46.48"
    assert metrics["Completed trades"] == "6"
    assert metrics["Win rate"] == "50.00%"
    assert metrics["Maximum drawdown"] == "1.28%"

    assert len(app.dataframe) == 1
    history = app.dataframe[0].value
    assert list(history["Side"]) == ["BUY", "SELL", "BUY"]
    assert list(history["Reason"]) == ["Dip Entry", "Take Profit", "Dip Entry"]
    assert any("PAPER TRADING ONLY" in element.value for element in app.markdown)
    assert app.warning[0].value == "READ ONLY — This dashboard cannot place or modify trades."


def test_public_ui_has_no_order_or_administrative_controls(monkeypatch):
    monkeypatch.delenv("PUBLIC_REPORT_URL", raising=False)
    monkeypatch.delenv("PUBLIC_REPORT_ALLOWED_HOST", raising=False)
    app = run_app()

    assert not app.exception
    labels = [button.label.lower() for button in app.button]
    assert labels == ["refresh data"]
    assert not app.text_input
    assert not app.number_input
    assert not app.text_area
    assert not app.get("data_editor")
    assert all(
        term not in label
        for label in labels
        for term in ("buy", "sell", "submit order", "start", "stop", "reset")
    )


def test_unavailable_source_fails_closed_without_trace_or_credential_prompt(monkeypatch):
    monkeypatch.setenv("PUBLIC_REPORT_URL", "http://reports.example.invalid/report.json")
    monkeypatch.setenv("PUBLIC_REPORT_ALLOWED_HOST", "reports.example.invalid")
    app = run_app()

    assert not app.exception
    assert [error.value for error in app.error] == ["Data temporarily unavailable"]
    assert not app.text_input
    visible = " ".join(
        [element.value for element in app.error]
        + [element.value for element in app.caption]
    ).lower()
    assert "private aws" in visible
    assert "traceback" not in visible
    assert "credential" not in visible


def test_dashboard_import_surface_has_no_project_mutation_capability():
    sources = [APP_PATH, *PUBLIC_PACKAGE.glob("*.py")]
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in sources)

    forbidden = (
        "import boto3",
        "from boto3",
        "lambda.invoke",
        "paper_order",
        "submit_paper_order",
        "put_item",
        "update_item",
        "transact_write",
        "put_object",
        "delete_object",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
        "st.data_editor",
        "st.form",
        "aws_access_key",
        "aws_secret",
        "aws_profile",
    )
    assert all(term not in text for term in forbidden)


def test_dashboard_surface_excludes_private_infrastructure_identifiers():
    sources = [APP_PATH, *PUBLIC_PACKAGE.glob("*.py")]
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in sources)

    forbidden = (
        "arn:aws",
        "account_id",
        "function_name",
        "table_name",
        "bucket_name",
        "stack_name",
        "cloudformation",
    )
    assert all(term not in text for term in forbidden)
