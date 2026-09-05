from __future__ import annotations

from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

from src.public_dashboard import data_source
from src.public_dashboard.data_source import PublicReportUnavailable
from src.public_dashboard.models import PublicPaperReport


st.set_page_config(
    page_title="Bitcoin paper trading observability",
    page_icon=":material/candlestick_chart:",
    layout="wide",
)


TIME_RANGES = {
    "24H": timedelta(hours=24),
    "7D": timedelta(days=7),
    "30D": timedelta(days=30),
    "All": None,
}


@st.cache_data(ttl="1m", max_entries=4, show_spinner=False)
def load_report_cached(source_url: str, allowed_host: str) -> dict:
    """Cache only the sanitized public document, never private infrastructure data."""
    return data_source.load_public_report(source_url, allowed_host).model_dump(mode="json")


def money(value: float) -> str:
    return f"${value:,.2f}"


def btc(value: float) -> str:
    return f"{value:,.8f} BTC"


def percent(value: float) -> str:
    return f"{value:.2%}"


def optional_count(value: int | None) -> str:
    return str(value) if value is not None else "Unavailable"


def optional_percent(value: float | None) -> str:
    return percent(value) if value is not None else "Unavailable"


def filtered_frames(
    report: PublicPaperReport,
    time_range: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    candles = pd.DataFrame([candle.model_dump() for candle in report.candles])
    if not candles.empty:
        candles["timestamp"] = pd.to_datetime(candles["timestamp"], utc=True)
    trades = pd.DataFrame([trade.model_dump() for trade in report.trades])
    if not trades.empty:
        trades["executed_at"] = pd.to_datetime(trades["executed_at"], utc=True)

    window = TIME_RANGES[time_range]
    if window is not None and not candles.empty:
        cutoff = candles["timestamp"].max() - window
        candles = candles[candles["timestamp"] >= cutoff]
        if not trades.empty:
            trades = trades[trades["executed_at"] >= cutoff]
    return candles, trades


def candlestick_chart(candles: pd.DataFrame, trades: pd.DataFrame) -> alt.LayerChart:
    direction = alt.condition(
        "datum.open <= datum.close",
        alt.value("#34D399"),
        alt.value("#F87171"),
    )
    x_axis = alt.X("timestamp:T", title=None, axis=alt.Axis(format="%b %d %H:%M"))
    price_axis = alt.Y(
        "low:Q",
        title="BTC-USD price",
        scale=alt.Scale(zero=False),
        axis=alt.Axis(format="$,.0f"),
    )
    wicks = (
        alt.Chart(candles)
        .mark_rule()
        .encode(
            x=x_axis,
            y=price_axis,
            y2="high:Q",
            color=direction,
            tooltip=[
                alt.Tooltip("timestamp:T", title="Time"),
                alt.Tooltip("open:Q", title="Open", format="$,.2f"),
                alt.Tooltip("high:Q", title="High", format="$,.2f"),
                alt.Tooltip("low:Q", title="Low", format="$,.2f"),
                alt.Tooltip("close:Q", title="Close", format="$,.2f"),
                alt.Tooltip("volume:Q", title="Volume", format=",.2f"),
            ],
        )
    )
    bodies = (
        alt.Chart(candles)
        .mark_bar(size=9)
        .encode(
            x=x_axis,
            y=alt.Y("open:Q", scale=alt.Scale(zero=False)),
            y2="close:Q",
            color=direction,
        )
    )
    layers: list[alt.Chart] = [wicks, bodies]
    if not trades.empty:
        markers = (
            alt.Chart(trades)
            .mark_point(filled=True, size=150, stroke="white", strokeWidth=1)
            .encode(
                x=alt.X("executed_at:T", title=None),
                y=alt.Y("price:Q", scale=alt.Scale(zero=False)),
                color=alt.Color(
                    "side:N",
                    title="Paper trade",
                    scale=alt.Scale(domain=["BUY", "SELL"], range=["#34D399", "#F87171"]),
                ),
                shape=alt.Shape(
                    "side:N",
                    scale=alt.Scale(domain=["BUY", "SELL"], range=["triangle-up", "triangle-down"]),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("executed_at:T", title="Executed"),
                    alt.Tooltip("side:N", title="Side"),
                    alt.Tooltip("reason:N", title="Reason"),
                    alt.Tooltip("price:Q", title="Paper fill", format="$,.2f"),
                    alt.Tooltip("quantity:Q", title="Quantity", format=".8f"),
                ],
            )
        )
        layers.append(markers)
    return alt.layer(*layers).properties(height=430).interactive(bind_y=False)


with st.container(
    horizontal=True,
    horizontal_alignment="distribute",
    vertical_alignment="center",
):
    st.title("Bitcoin paper agent", icon=":material/candlestick_chart:")
    if st.button("Refresh data", icon=":material/refresh:", type="tertiary"):
        load_report_cached.clear()
        st.rerun()

st.warning(
    "READ ONLY — This dashboard cannot place or modify trades.",
    icon=":material/visibility:",
)
st.markdown("**PAPER TRADING ONLY · PUBLIC READ-ONLY INTERFACE**")

with st.sidebar:
    st.header("View", icon=":material/tune:")
    time_range = st.segmented_control(
        "Chart timeframe",
        options=list(TIME_RANGES),
        default="24H",
        required=True,
        key="timeframe",
        bind="query-params",
    )
    st.caption("Filters affect only this display. They cannot change the paper agent.")
    st.badge("Public reporting only", color="blue", icon=":material/verified_user:")
    st.badge("No AWS credentials", color="green", icon=":material/key_off:")
    st.badge("No trading controls", color="green", icon=":material/block:")

source_url, allowed_host = data_source.configured_source()
try:
    report = PublicPaperReport.model_validate(load_report_cached(source_url, allowed_host))
except PublicReportUnavailable as exc:
    st.error(str(exc), icon=":material/cloud_off:")
    st.caption(
        "The public report could not be loaded. This dashboard will not fall back to private AWS access."
    )
    st.stop()

with st.container(horizontal=True, vertical_alignment="center"):
    report_age = datetime.now(timezone.utc) - report.updated_at.astimezone(timezone.utc)
    feed_is_fresh = (
        report.data_status == "LIVE"
        and timedelta(minutes=-2) <= report_age <= timedelta(minutes=15)
    )
    if feed_is_fresh:
        st.badge(
            "LIVE PROJECT DATA — PAPER TRADING ONLY",
            color="green",
            icon=":material/sensors:",
        )
    elif report.data_status == "SAMPLE":
        st.badge(
            "SANITIZED SAMPLE DATA — PAPER TRADING ONLY",
            color="orange",
            icon=":material/science:",
        )
    else:
        st.badge(
            "LIVE PROJECT DATA STALE — LAST CONFIRMED SNAPSHOT",
            color="orange",
            icon=":material/schedule:",
        )
    displayed_agent_status = report.agent_status if report.data_status == "SAMPLE" or feed_is_fresh else "DEGRADED"
    status_color = "green" if displayed_agent_status == "RUNNING" else "orange"
    st.badge(
        f"Bot status: {displayed_agent_status}",
        color=status_color,
        icon=":material/smart_toy:",
    )
    st.caption(f"Last report update: {report.updated_at.astimezone().strftime('%b %d, %Y %H:%M:%S %Z')}")

with st.container(horizontal=True):
    st.metric("Mode", "PAPER", border=True)
    st.metric("Total equity", money(report.portfolio.total_equity), border=True)
    st.metric("Available cash", money(report.portfolio.available_cash), border=True)
    st.metric("BTC holding", btc(report.portfolio.btc_quantity), border=True)

view = st.segmented_control(
    "Workspace",
    options=["Overview", "Market", "Automatic strategy", "Performance", "Risk & history"],
    default="Overview",
    key="workspace_view",
    bind="query-params",
)

candle_frame, marker_frame = filtered_frames(report, str(time_range))

if view == "Overview":
    left, right = st.columns([3, 2])
    with left.container(border=True, height="stretch"):
        st.subheader("Paper portfolio", icon=":material/account_balance_wallet:")
        with st.container(horizontal=True):
            st.metric("Starting balance", money(report.portfolio.starting_cash))
            st.metric("Current BTC price", money(report.portfolio.current_price))
            st.metric("Average entry", money(report.portfolio.avg_entry_price))
        with st.container(horizontal=True):
            st.metric("Realized P&L", money(report.portfolio.realized_pnl))
            st.metric("Unrealized P&L", money(report.portfolio.unrealized_pnl))
            st.metric("Position", report.position.status)
    with right.container(border=True, height="stretch"):
        st.subheader("Public safety boundaries", icon=":material/shield:")
        st.markdown(
            "- BTC-USD only\n"
            "- Paper execution only\n"
            "- No AWS credentials\n"
            "- No private exchange credentials\n"
            "- No order or configuration controls"
        )
        st.caption("This page displays a sanitized reporting document only.")

elif view == "Market":
    with st.container(border=True):
        st.subheader("BTC-USD paper-trading timeline", icon=":material/candlestick_chart:")
        if candle_frame.empty:
            st.info(
                "Historical candles are not included in the live sanitized status feed.",
                icon=":material/info:",
            )
            st.metric("Latest BTC-USD price", money(report.portfolio.current_price))
        else:
            st.caption(
                "Public market candles with simulated BUY and SELL markers. "
                "Zoom and pan are display-only."
            )
            st.altair_chart(candlestick_chart(candle_frame, marker_frame), key="paper_candles")
    with st.container(border=True):
        st.subheader("Current paper position", icon=":material/account_balance_wallet:")
        with st.container(horizontal=True):
            st.metric("Position", report.position.status)
            st.metric("Quantity", btc(report.position.quantity))
            st.metric("Average entry", money(report.position.entry_price))
            st.metric("Unrealized P&L", money(report.position.unrealized_pnl))
        if report.position.status == "OPEN":
            st.table(
                {
                    "Current price": money(report.position.current_price),
                    "Take-profit level": money(report.position.take_profit_price or 0),
                    "Stop-loss level": money(report.position.stop_loss_price or 0),
                    "Trailing-stop level": money(report.position.trailing_stop_price or 0),
                },
                border="horizontal",
                width="stretch",
            )

elif view == "Automatic strategy":
    st.warning(
        "PAPER TRADING / SIMULATION ONLY — this public page cannot place an order.",
        icon=":material/science:",
    )
    with st.container(horizontal=True):
        st.metric("Automatic dip buy", report.strategy.status, border=True)
        scheduler_status = (
            report.strategy.scheduler_status
            if report.data_status == "SAMPLE" or feed_is_fresh
            else "UNKNOWN"
        )
        st.metric("Scheduler", scheduler_status, border=True)
        st.metric("Paper mode", "ON", border=True)
    with st.container(horizontal=True):
        st.metric("Evaluation frequency", report.strategy.evaluation_frequency, border=True)
        st.metric(
            "Last evaluation",
            report.strategy.last_evaluated_at.astimezone().strftime("%b %d, %Y %H:%M %Z"),
            border=True,
        )
        st.metric("Last result", report.strategy.last_result, border=True)
    with st.container(horizontal=True):
        st.metric("Current BTC price", money(report.portfolio.current_price), border=True)
        st.metric("60-minute reference", money(report.strategy.reference_price), border=True)
        st.metric("Measured dip", percent(report.strategy.measured_dip_pct), border=True)

    config_col, evaluation_col = st.columns(2)
    with config_col.container(border=True, height="stretch"):
        st.subheader("Fixed configuration")
        st.table(
            {
                "Pair": report.symbol,
                "Dip threshold": percent(report.strategy.threshold_pct),
                "Lookback": f"{report.strategy.lookback_minutes} minutes",
                "Paper order": money(report.strategy.order_size_usd),
                "Cooldown": f"{report.strategy.cooldown_minutes} minutes",
                "Decision source": report.strategy.decision_source.replace("_", " ").title(),
            },
            border="horizontal",
            width="stretch",
        )
    with evaluation_col.container(border=True, height="stretch"):
        st.subheader("Latest evaluation")
        st.metric("Result", report.strategy.last_result)
        st.write(report.strategy.latest_decision)
        st.write(f"Signal: {report.strategy.signal}")
        st.write(f"Automatic exit: {report.strategy.automatic_exit_status}")

elif view == "Performance":
    with st.container(horizontal=True):
        st.metric("Total return", percent(report.performance.return_pct), border=True)
        st.metric("Completed sells", optional_count(report.performance.completed_trades), border=True)
        st.metric("Wins", optional_count(report.performance.wins), border=True)
        st.metric("Losses", optional_count(report.performance.losses), border=True)
        st.metric("Win rate", optional_percent(report.performance.win_rate), border=True)
        st.metric("Maximum drawdown", percent(report.performance.max_drawdown_pct), border=True)
    st.caption("Each sell, including a partial sell, counts as one result. Wins and losses use "
               "average entry cost including buy and sell fees. Open buys are not wins. "
               "Counts cover the complete saved history; the table shows up to 500 recent fills.")
    if report.performance.completed_trades is None:
        st.info("The public feed has not supplied complete trade history. Unavailable does not mean zero trades.")
    with st.container(border=True):
        st.subheader("Paper equity comparison", icon=":material/query_stats:")
        equity = pd.DataFrame(
            {
                "Account value": ["Starting paper balance", "Current paper equity"],
                "USD": [report.portfolio.starting_cash, report.portfolio.total_equity],
            }
        )
        st.bar_chart(equity, x="Account value", y="USD", horizontal=True)

elif view == "Risk & history":
    risk_col, history_col = st.columns([2, 3])
    with risk_col.container(border=True, height="stretch"):
        st.subheader("Risk-control status", icon=":material/shield:")
        risk_color = {"NORMAL": "green", "CAUTION": "orange", "HALTED": "red"}[
            report.risk.status
        ]
        st.badge(report.risk.status, color=risk_color, icon=":material/health_and_safety:")
        st.table(
            {
                "Maximum paper position": money(report.risk.max_position_usd),
                "Daily-loss limit": percent(report.risk.daily_loss_limit_pct),
                "Drawdown limit": percent(report.risk.max_drawdown_limit_pct),
                "Current drawdown": percent(report.risk.current_drawdown_pct),
            },
            border="horizontal",
            width="stretch",
        )
        if report.risk.controls_triggered:
            st.warning(
                "Triggered controls: " + ", ".join(report.risk.controls_triggered),
                icon=":material/warning:",
            )
        else:
            st.caption("No risk controls are currently triggered.")

    with history_col.container(border=True, height="stretch"):
        st.subheader("Recent paper trades", icon=":material/receipt_long:")
        if report.trades:
            history = pd.DataFrame(
                [
                    {
                        "Executed": trade.executed_at,
                        "Side": trade.side,
                        "Reason": trade.reason.replace("_", " ").title(),
                        "Price": trade.price,
                        "BTC quantity": trade.quantity,
                        "Paper notional": trade.notional,
                        "Realized P&L": trade.realized_pnl,
                        "Fees": trade.fees,
                        "Result": ("Entry" if trade.side == "BUY" else "Unavailable" if trade.realized_pnl is None
                                   else "Break-even" if abs(trade.realized_pnl) < 1e-10
                                   else "Win" if trade.realized_pnl > 0 else "Loss"),
                    }
                    for trade in reversed(report.trades)
                ]
            )
            st.dataframe(
                history,
                hide_index=True,
                height=300,
                key="paper_trade_history",
                column_config={
                    "Executed": st.column_config.DatetimeColumn(format="MMM DD, YYYY HH:mm"),
                    "Price": st.column_config.NumberColumn(format="$%.2f"),
                    "BTC quantity": st.column_config.NumberColumn(format="%.8f"),
                    "Paper notional": st.column_config.NumberColumn(format="$%.2f"),
                    "Realized P&L": st.column_config.NumberColumn("Net sell P&L", format="$%.6f"),
                    "Fees": st.column_config.NumberColumn(format="$%.6f"),
                },
            )
        else:
            st.caption("No completed paper trades are available in the public report.")

st.caption(
    "Educational observability surface only. No real-money execution, financial advice, or profitability guarantee."
)
