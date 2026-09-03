from __future__ import annotations

from datetime import timedelta

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


def filtered_frames(
    report: PublicPaperReport,
    time_range: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    candles = pd.DataFrame([candle.model_dump() for candle in report.candles])
    candles["timestamp"] = pd.to_datetime(candles["timestamp"], utc=True)
    trades = pd.DataFrame([trade.model_dump() for trade in report.trades])
    if not trades.empty:
        trades["executed_at"] = pd.to_datetime(trades["executed_at"], utc=True)

    window = TIME_RANGES[time_range]
    if window is not None:
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
    st.title("Bitcoin Paper Trading Agent", icon=":material/candlestick_chart:")
    if st.button("Refresh data", icon=":material/refresh:", type="tertiary"):
        load_report_cached.clear()
        st.rerun()

st.warning(
    "READ ONLY — This dashboard cannot place or modify trades.",
    icon=":material/visibility:",
)
st.markdown("**PAPER TRADING ONLY · PUBLIC READ-ONLY OBSERVABILITY**")

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
except PublicReportUnavailable:
    st.error("Data temporarily unavailable", icon=":material/cloud_off:")
    st.caption(
        "The public report could not be loaded. This dashboard will not fall back to private AWS access."
    )
    st.stop()

with st.container(horizontal=True, vertical_alignment="center"):
    if report.data_status == "LIVE":
        st.badge(
            "LIVE PROJECT DATA — PAPER TRADING ONLY",
            color="green",
            icon=":material/sensors:",
        )
    else:
        st.badge(
            "SANITIZED SAMPLE DATA — PAPER TRADING ONLY",
            color="orange",
            icon=":material/science:",
        )
    status_color = "green" if report.agent_status == "RUNNING" else "orange"
    st.badge(
        f"Bot status: {report.agent_status}",
        color=status_color,
        icon=":material/smart_toy:",
    )
    st.caption(f"Last report update: {report.updated_at.astimezone().strftime('%b %d, %Y %H:%M:%S %Z')}")

closes = [candle.close for candle in report.candles[-24:]]
with st.container(horizontal=True):
    st.metric(
        "BTC-USD",
        money(report.portfolio.current_price),
        border=True,
        chart_data=closes,
        chart_type="line",
    )
    st.metric("Paper account equity", money(report.portfolio.total_equity), border=True)
    st.metric("Simulated cash", money(report.portfolio.available_cash), border=True)
    st.metric("Simulated BTC", btc(report.portfolio.btc_quantity), border=True)
    st.metric(
        "Total return",
        percent(report.performance.return_pct),
        border=True,
    )

candle_frame, marker_frame = filtered_frames(report, str(time_range))
with st.container(border=True):
    st.subheader("BTC-USD paper-trading timeline", icon=":material/candlestick_chart:")
    st.caption("Public market candles with simulated BUY and SELL markers. Zoom and pan are display-only.")
    st.altair_chart(candlestick_chart(candle_frame, marker_frame), key="paper_candles")

left, right = st.columns([3, 2])
with left.container(border=True, height="stretch"):
    st.subheader("Current paper position", icon=":material/account_balance_wallet:")
    with st.container(horizontal=True):
        st.metric("Position", report.position.status)
        st.metric("Quantity", btc(report.position.quantity))
        st.metric("Average entry", money(report.position.entry_price))
        st.metric("Unrealized P&L", money(report.position.unrealized_pnl))
    if report.position.status == "OPEN":
        levels = {
            "Current price": money(report.position.current_price),
            "Take-profit level": money(report.position.take_profit_price or 0),
            "Stop-loss level": money(report.position.stop_loss_price or 0),
            "Trailing-stop level": money(report.position.trailing_stop_price or 0),
        }
        st.table(levels, border="horizontal", width="stretch")

with right.container(border=True, height="stretch"):
    st.subheader("Strategy status", icon=":material/strategy:")
    with st.container(horizontal=True):
        st.badge(report.strategy.status, color="green" if report.strategy.status == "ENABLED" else "gray")
        st.badge(f"Signal: {report.strategy.signal}", color="blue")
        st.badge(f"Auto exit: {report.strategy.automatic_exit_status}", color="violet")
    st.markdown(f"**{report.strategy.name}**")
    st.write(report.strategy.latest_decision)
    st.caption(
        f"Last evaluated: {report.strategy.last_evaluated_at.astimezone().strftime('%b %d, %Y %H:%M:%S %Z')}"
    )

st.subheader("Paper performance", icon=":material/query_stats:")
with st.container(horizontal=True):
    st.metric("Completed trades", str(report.performance.completed_trades), border=True)
    st.metric("Wins", str(report.performance.wins), border=True)
    st.metric("Losses", str(report.performance.losses), border=True)
    st.metric("Win rate", percent(report.performance.win_rate), border=True)
    st.metric("Maximum drawdown", percent(report.performance.max_drawdown_pct), border=True)
    st.metric("Realized P&L", money(report.portfolio.realized_pnl), border=True)

risk_col, history_col = st.columns([2, 3])
with risk_col.container(border=True, height="stretch"):
    st.subheader("Risk-control status", icon=":material/shield:")
    risk_color = {"NORMAL": "green", "CAUTION": "orange", "HALTED": "red"}[report.risk.status]
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
                "Realized P&L": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
    else:
        st.caption("No completed paper trades are available in the public report.")

st.caption(
    "Educational observability surface only. No real-money execution, financial advice, or profitability guarantee."
)
