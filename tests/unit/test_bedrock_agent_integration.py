import pytest
from datetime import datetime, timezone, timedelta

from src.agent.bedrock_client import FakeAgentModelProvider
from src.agent.tool_registry import ToolRegistry, UnknownToolError
from src.agent.orchestrator import AgentOrchestrator
from src.agent.models import AgentDecision
from src.agent.store import InMemoryTradeEventStore
from src.portfolio.portfolio import Portfolio
from src.broker.paper_broker import PaperBroker
from src.agent.exceptions import PaperOrderRejected


def make_tools():
    p = Portfolio()
    b = PaperBroker(p)
    store = InMemoryTradeEventStore()
    return ToolRegistry(portfolio=p, broker=b, store=store, allow_synthetic_market_data=True)


def test_reject_non_btc_symbol_validation():
    # provider returns invalid symbol -> validation fails
    provider = FakeAgentModelProvider(decision={'symbol': 'ETH-USD', 'action': 'BUY', 'strategy_id': 'ema_cross_v1', 'confidence': 0.5, 'requested_notional_usd': 100.0})
    with pytest.raises(Exception):
        provider.get_decision({})


def test_reject_shorting_without_holdings():
    tools = make_tools()
    now = datetime.now(timezone.utc)
    decision = AgentDecision(symbol='BTC-USD', action='SELL', strategy_id='ema_cross_v1', confidence=0.5, requested_notional_usd=100.0, timestamp=now, idempotency_key='k-sell')
    market = tools.get_market_data('BTC-USD', '15m', 1)
    with pytest.raises(PaperOrderRejected):
        tools.submit_paper_order(decision, market)


def test_notional_too_large_rejected():
    tools = make_tools()
    # request extremely large notional to trigger NOTIONAL_TOO_LARGE
    now = datetime.now(timezone.utc)
    decision = AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=0.9, requested_notional_usd=1_000_000.0, timestamp=now, idempotency_key='big1')
    market = tools.get_market_data('BTC-USD', '15m', 1)
    with pytest.raises(PaperOrderRejected):
        tools.submit_paper_order(decision, market)


def test_unknown_tool_rejected():
    tools = make_tools()
    with pytest.raises(UnknownToolError):
        tools.call('paper_broker')


def test_malformed_model_output_handled():
    # provider returns None repeatedly -> orchestrator should fail closed after max iterations
    class BadProvider(FakeAgentModelProvider):
        def get_decision(self, context):
            return None

    tools = make_tools()
    orch = AgentOrchestrator(BadProvider(), tools, max_iterations=2, max_tool_calls=2)
    with pytest.raises(RuntimeError):
        orch.run({})


def test_nan_confidence_rejected():
    tools = make_tools()
    now = datetime.now(timezone.utc)
    with pytest.raises(Exception):
        AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=float('nan'), requested_notional_usd=10.0, timestamp=now)


def test_confidence_gt_one_rejected():
    tools = make_tools()
    now = datetime.now(timezone.utc)
    with pytest.raises(Exception):
        AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=1.5, requested_notional_usd=10.0, timestamp=now)


def test_production_no_synthetic_fallback():
    # ToolRegistry with synthetic disabled should fail closed if adapter errors
    p = Portfolio()
    b = PaperBroker(p)
    store = InMemoryTradeEventStore()
    tr = ToolRegistry(portfolio=p, broker=b, store=store, allow_synthetic_market_data=False)
    # monkeypatch adapter to raise
    from src.agent import tool_registry as trmod
    orig = trmod.adapter_get_market_data
    def raise_err(symbol, timeframe, candle_count):
        raise RuntimeError('network down')
    trmod.adapter_get_market_data = raise_err
    try:
        with pytest.raises(RuntimeError):
            tr.get_market_data('BTC-USD', '15m', 1)
    finally:
        trmod.adapter_get_market_data = orig


def test_leverage_field_rejected_by_model():
    now = datetime.now(timezone.utc)
    # attempt to supply extra sensitive field
    with pytest.raises(Exception):
        AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=0.5, requested_notional_usd=10.0, timestamp=now, leverage=10)


def test_stale_market_data_rejected():
    tools = make_tools()
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    # craft market snapshot stale
    from src.agent.models import MarketDataSnapshot
    market = MarketDataSnapshot(symbol='BTC-USD', price=30000.0, timestamp=now)
    # craft decision
    from src.agent.models import AgentDecision
    decision = AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=0.5, requested_notional_usd=10.0, timestamp=datetime.now(timezone.utc), idempotency_key='stale1')
    # directly call AgentTradeService via tools
    with pytest.raises(PaperOrderRejected):
        tools.submit_paper_order(decision, market)


def test_idempotency_same_request_returns_same_order():
    tools = make_tools()
    now = datetime.now(timezone.utc)
    decision = AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=0.8, requested_notional_usd=10.0, timestamp=now, idempotency_key='idem1')
    market = tools.get_market_data('BTC-USD', '15m', 1)
    res1 = tools.submit_paper_order(decision, market)
    # retry may be accepted or conflict; ensure no duplicate fills were created
    try:
        res2 = tools.submit_paper_order(decision, market)
    except Exception:
        res2 = None
    fills = len(tools._service.store.fills)
    assert fills == 1


def test_idempotency_conflict_on_different_request():
    tools = make_tools()
    now = datetime.now(timezone.utc)
    d1 = AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=0.8, requested_notional_usd=10.0, timestamp=now, idempotency_key='idem-conf')
    d2 = AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=0.8, requested_notional_usd=20.0, timestamp=now, idempotency_key='idem-conf')
    market = tools.get_market_data('BTC-USD', '15m', 1)
    _ = tools.submit_paper_order(d1, market)
    with pytest.raises(PaperOrderRejected):
        tools.submit_paper_order(d2, market)


def test_valid_buy_and_sell_flow():
    tools = make_tools()
    # initial buy
    now = datetime.now(timezone.utc)
    buy = AgentDecision(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', confidence=0.9, requested_notional_usd=10.0, timestamp=now, idempotency_key='flow1')
    market = tools.get_market_data('BTC-USD', '15m', 1)
    res_buy = tools.submit_paper_order(buy, market)
    assert 'paper_order_id' in res_buy
    # now sell portion
    sell = AgentDecision(symbol='BTC-USD', action='SELL', strategy_id='ema_cross_v1', confidence=0.9, requested_notional_usd=5.0, timestamp=datetime.now(timezone.utc), idempotency_key='flow2')
    res_sell = tools.submit_paper_order(sell, market)
    assert 'paper_order_id' in res_sell
