from datetime import datetime, timezone

import pytest

from src.agent import lambda_handler as lh
from src.agent.models import AgentDecision, MarketDataSnapshot
from src.agent.store import InMemoryTradeEventStore
from src.portfolio.portfolio import Portfolio


def set_safe_env(monkeypatch):
    monkeypatch.setenv('PAPER_MODE', 'true')
    monkeypatch.setenv('SYMBOL', 'BTC-USD')
    monkeypatch.setenv('ALLOW_SYNTHETIC_MARKET_DATA', 'false')
    monkeypatch.setenv('BEDROCK_MODEL_ID', lh.EXPECTED_MODEL_ID)
    monkeypatch.setenv('TRADE_EVENTS_TABLE', 'test-table')
    monkeypatch.setenv('AWS_REGION', 'us-west-2')


def market_fixture():
    now = datetime.now(timezone.utc)
    return (
        MarketDataSnapshot(symbol='BTC-USD', price=30_000.0, timestamp=now),
        {
            'source': 'coinbase',
            'symbol': 'BTC-USD',
            'timeframe': '15m',
            'candle_count': 20,
            'latest_candle_at': now.isoformat(),
            'retrieved_at': now.isoformat(),
            'latest_price': 30_000.0,
        },
    )


def test_env_enforced(monkeypatch):
    for key in ['PAPER_MODE', 'SYMBOL', 'ALLOW_SYNTHETIC_MARKET_DATA', 'BEDROCK_MODEL_ID', 'TRADE_EVENTS_TABLE']:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError):
        lh.lambda_handler({'action': 'health'}, None)


def test_analysis_is_no_trade_and_has_no_order_tool(monkeypatch):
    set_safe_env(monkeypatch)
    monkeypatch.setattr(lh, '_load_state', lambda: (InMemoryTradeEventStore(), Portfolio()))
    monkeypatch.setattr(lh, '_fresh_market_snapshot', market_fixture)

    class FakeProvider:
        def __init__(self, config, tools, analysis_only=False):
            assert 'submit_paper_order' not in tools.tools
            assert analysis_only is True

        def get_decision(self, context):
            return AgentDecision(
                symbol='BTC-USD',
                action='HOLD',
                strategy_id='ema_cross_v1',
                confidence=0.5,
                requested_notional_usd=0.0,
                timestamp=datetime.now(timezone.utc),
            )

    monkeypatch.setattr(lh, 'BedrockAgentModelProvider', FakeProvider)
    out = lh.lambda_handler({'action': 'analyze'}, None)
    assert out['result'] == 'HOLD'
    assert out['reason'] == 'ANALYSIS_ONLY'


def test_analysis_suppresses_actionable_model_output(monkeypatch):
    set_safe_env(monkeypatch)
    monkeypatch.setattr(lh, '_load_state', lambda: (InMemoryTradeEventStore(), Portfolio()))
    monkeypatch.setattr(lh, '_fresh_market_snapshot', market_fixture)

    class FakeProvider:
        def __init__(self, config, tools, analysis_only=False):
            assert analysis_only is True

        def get_decision(self, context):
            return AgentDecision(
                symbol='BTC-USD',
                action='BUY',
                strategy_id='ema_cross_v1',
                confidence=0.9,
                requested_notional_usd=100.0,
                timestamp=datetime.now(timezone.utc),
            )

    monkeypatch.setattr(lh, 'BedrockAgentModelProvider', FakeProvider)
    out = lh.lambda_handler({'action': 'analyze'}, None)
    assert out['result'] == 'NO_TRADE'
    assert out['reason'] == 'ACTION_SUPPRESSED_ANALYSIS_ONLY'


def test_pristine_risk_baseline_without_snapshot_recovers_starting_portfolio(monkeypatch):
    set_safe_env(monkeypatch)

    class BaselineStore:
        def __init__(self, **kwargs):
            pass

        def get_portfolio_risk_state(self, portfolio_id):
            return {
                'version': 1,
                'state': {
                    'portfolio_id': 'default',
                    'current_equity': 10000.0,
                    'peak_equity': 10000.0,
                    'day_start_equity': 10000.0,
                    'daily_pnl': 0.0,
                    'current_drawdown': 0.0,
                },
            }

        def get_latest_portfolio_snapshot(self, portfolio_id):
            return None

    monkeypatch.setattr(lh, 'DynamoTradeEventStore', BaselineStore)
    _, portfolio = lh._load_state()
    assert portfolio.cash == 10000.0
    assert portfolio.btc_quantity == 0.0


def test_non_pristine_risk_state_without_snapshot_fails_closed(monkeypatch):
    set_safe_env(monkeypatch)

    class IncompleteStore:
        def __init__(self, **kwargs):
            pass

        def get_portfolio_risk_state(self, portfolio_id):
            return {
                'version': 2,
                'state': {
                    'portfolio_id': 'default',
                    'current_equity': 9999.0,
                    'peak_equity': 10000.0,
                    'day_start_equity': 10000.0,
                    'daily_pnl': -1.0,
                    'current_drawdown': 0.0001,
                },
            }

        def get_latest_portfolio_snapshot(self, portfolio_id):
            return None

    monkeypatch.setattr(lh, 'DynamoTradeEventStore', IncompleteStore)
    with pytest.raises(RuntimeError, match='PORTFOLIO_SNAPSHOT_UNAVAILABLE'):
        lh._load_state()


def test_market_failure_returns_no_trade(monkeypatch):
    set_safe_env(monkeypatch)
    monkeypatch.setattr(lh, '_load_state', lambda: (InMemoryTradeEventStore(), Portfolio()))
    monkeypatch.setattr(lh, '_fresh_market_snapshot', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    out = lh.lambda_handler({'action': 'analyze'}, None)
    assert out['result'] == 'NO_TRADE'
    assert out['reason'] == 'MARKET_DATA_UNAVAILABLE'


def test_paper_order_commits_candidate_snapshot_and_is_idempotent(monkeypatch):
    set_safe_env(monkeypatch)
    store = InMemoryTradeEventStore()

    def load_state():
        snapshot = store.get_latest_portfolio_snapshot('default')
        portfolio = Portfolio.from_snapshot(snapshot) if snapshot else Portfolio()
        return store, portfolio

    monkeypatch.setattr(lh, '_load_state', load_state)
    monkeypatch.setattr(lh, '_fresh_market_snapshot', market_fixture)
    event = {
        'action': 'paper_order',
        'proposal': {
            'symbol': 'BTC-USD',
            'action': 'BUY',
            'strategy_id': 'ema_cross_v1',
            'requested_notional_usd': 10.0,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'idempotency_key': 'lambda-smoke-1',
            'allow_risk_resizing': True,
        },
    }
    first = lh.lambda_handler(event, None)
    second = lh.lambda_handler(event, None)
    assert first['result'] == 'PAPER_ORDER_COMMITTED'
    assert second['paper_order']['fill_id'] == first['paper_order']['fill_id']
    assert len(store.fills) == 1
    assert store.get_latest_portfolio_snapshot('default')['btc_quantity'] > 0


def test_unsupported_action_rejected(monkeypatch):
    set_safe_env(monkeypatch)
    with pytest.raises(ValueError):
        lh.lambda_handler({'action': 'live_order'}, None)


@pytest.mark.parametrize(
    'unsafe_field,unsafe_value',
    [('symbol', 'ETH-USD'), ('leverage', 2)],
)
def test_paper_order_rejects_unsafe_proposal_fields(monkeypatch, unsafe_field, unsafe_value):
    set_safe_env(monkeypatch)
    monkeypatch.setattr(lh, '_load_state', lambda: (InMemoryTradeEventStore(), Portfolio()))
    monkeypatch.setattr(lh, '_fresh_market_snapshot', market_fixture)
    proposal = {
        'symbol': 'BTC-USD',
        'action': 'BUY',
        'strategy_id': 'ema_cross_v1',
        'requested_notional_usd': 10.0,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'idempotency_key': 'unsafe-1',
    }
    proposal[unsafe_field] = unsafe_value
    out = lh.lambda_handler({'action': 'paper_order', 'proposal': proposal}, None)
    assert out['result'] == 'REJECTED'
    assert out['reason_code'] == (
        'INVALID_ASSET' if unsafe_field == 'symbol' else 'LEVERAGE_NOT_ALLOWED'
    )


def test_paper_order_rejects_short_without_holdings(monkeypatch):
    set_safe_env(monkeypatch)
    monkeypatch.setattr(lh, '_load_state', lambda: (InMemoryTradeEventStore(), Portfolio()))
    monkeypatch.setattr(lh, '_fresh_market_snapshot', market_fixture)
    event = {
        'action': 'paper_order',
        'proposal': {
            'symbol': 'BTC-USD',
            'action': 'SELL',
            'strategy_id': 'ema_cross_v1',
            'requested_notional_usd': 0.0,
            'requested_quantity': 0.001,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'idempotency_key': 'short-1',
        },
    }
    out = lh.lambda_handler(event, None)
    assert out['result'] == 'REJECTED'
    assert out['reason_code'] == 'INVALID_QUANTITY'
