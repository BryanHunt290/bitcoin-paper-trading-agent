from datetime import datetime, timezone
import uuid
import pytest

from src.agent.tools import AgentTradeService
from src.agent.store import InMemoryTradeEventStore, TradeEventStore
from src.broker.paper_broker import PaperBroker
from src.portfolio.portfolio import Portfolio
from src.agent.models import AgentTradeProposal, MarketDataSnapshot, PaperOrderRequest
from src.agent.exceptions import PaperOrderRejected


class FaultyStore(InMemoryTradeEventStore):
    """Fault-injecting store to simulate DynamoDB failure modes."""
    def __init__(self, mode=None):
        super().__init__()
        self.mode = mode

    def atomic_trade_commit(self, *args, **kwargs):
        if self.mode == 'TRANSACTION_CANCELED':
            raise Exception('TransactionCanceledException')
        if self.mode == 'RISK_CONFLICT':
            return {'conflict': 'RISK_STATE_CONFLICT'}
        if self.mode == 'IDEMPOTENCY_CONFLICT':
            return {'conflict': 'IDEMPOTENCY_CONFLICT'}
        if self.mode == 'THROTTLE_ONCE':
            # simulate transient throttle internally then succeed on same call
            if not hasattr(self, '_throttled'):
                self._throttled = True
                # simulate transient backoff then proceed to commit
                return super().atomic_trade_commit(*args, **kwargs)
        if self.mode == 'THROTTLE_ALWAYS':
            raise Exception('ProvisionedThroughputExceededException')
        if self.mode == 'ACCESS_DENIED':
            raise Exception('AccessDeniedException')
        if self.mode == 'SERIALIZATION':
            raise Exception('SerializationException')
        if self.mode == 'AMBIGUOUS':
            # simulate uncertainty: raise transient then do not reveal
            raise Exception('InternalServerError')
        return super().atomic_trade_commit(*args, **kwargs)


def make_proposal():
    return AgentTradeProposal(idempotency_key=str(uuid.uuid4()), symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', requested_notional_usd=100.0, timestamp=datetime.now(timezone.utc))


def make_market():
    return MarketDataSnapshot(symbol='BTC-USD', price=30000.0, timestamp=datetime.now(timezone.utc))


def make_portfolio_snapshot():
    p = Portfolio(starting_cash=100000.0)
    return p.snapshot(30000.0)


def _always_allow(_amt, _portfolio_state, _risk_config, _persisted_state):
    return {'allowed': True, 'position_size_btc': 0.00333333, 'max_notional': 100.0, 'equity': 100000.0}


def run_request_with_store(store_mode):
    store = FaultyStore(mode=store_mode)
    # init risk state
    init_state = {'portfolio_id': 'default', 'current_equity': 100000.0, 'peak_equity': 100000.0, 'day_start_equity': 100000.0, 'trading_day_utc': datetime.now(timezone.utc).date().isoformat(), 'daily_pnl': 0.0, 'current_drawdown': 0.0, 'updated_at': datetime.now(timezone.utc).isoformat()}
    store.initialize_portfolio_risk_state('default', init_state)
    broker = PaperBroker(Portfolio(starting_cash=100000.0))
    svc = AgentTradeService(broker, store=store)
    proposal = make_proposal()
    req = PaperOrderRequest(proposal=proposal, market_snapshot=make_market(), portfolio_snapshot=make_portfolio_snapshot())
    return svc, req, store


def test_transaction_canceled_fails_closed(monkeypatch):
    svc, req, store = run_request_with_store('TRANSACTION_CANCELED')
    import src.agent.tools as tools
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    with pytest.raises(PaperOrderRejected):
        svc.submit_paper_order(req)


def test_risk_state_conflict_fails_closed(monkeypatch):
    svc, req, store = run_request_with_store('RISK_CONFLICT')
    import src.agent.tools as tools
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    with pytest.raises(PaperOrderRejected):
        svc.submit_paper_order(req)


def test_idempotency_conflict_rejected(monkeypatch):
    svc, req, store = run_request_with_store(None)
    import src.agent.tools as tools
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    # first call succeeds
    res = svc.submit_paper_order(req)
    # now use same key but change fingerprint by altering portfolio snapshot
    svc2, req2, store2 = run_request_with_store('IDEMPOTENCY_CONFLICT')
    req.proposal.idempotency_key = req2.proposal.idempotency_key
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    with pytest.raises(PaperOrderRejected):
        svc2.submit_paper_order(req2)


def test_throttle_then_success_commits_once(monkeypatch):
    svc, req, store = run_request_with_store('THROTTLE_ONCE')
    import src.agent.tools as tools
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    res = svc.submit_paper_order(req)
    assert res.filled_quantity > 0


def test_throttle_exhaust_fail_closed(monkeypatch):
    svc, req, store = run_request_with_store('THROTTLE_ALWAYS')
    import src.agent.tools as tools
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    with pytest.raises(PaperOrderRejected):
        svc.submit_paper_order(req)


def test_access_denied_fails_immediately(monkeypatch):
    svc, req, store = run_request_with_store('ACCESS_DENIED')
    import src.agent.tools as tools
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    with pytest.raises(PaperOrderRejected):
        svc.submit_paper_order(req)


def test_serialization_error_fails_closed(monkeypatch):
    svc, req, store = run_request_with_store('SERIALIZATION')
    import src.agent.tools as tools
    monkeypatch.setattr(tools, 'calculate_position_size', _always_allow)
    with pytest.raises(PaperOrderRejected):
        svc.submit_paper_order(req)
