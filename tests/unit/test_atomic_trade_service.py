from datetime import datetime, timezone
import uuid
from src.agent.tools import AgentTradeService
from src.agent.store import InMemoryTradeEventStore
from src.broker.paper_broker import PaperBroker
from src.portfolio.portfolio import Portfolio
from src.agent.models import AgentTradeProposal, MarketDataSnapshot, PaperOrderRequest
from src.agent.exceptions import PaperOrderRejected


def make_proposal(action='BUY', quantity=None, notional=100.0):
    return AgentTradeProposal(
        idempotency_key=str(uuid.uuid4()),
        symbol='BTC-USD',
        action=action,
        requested_quantity=quantity,
        requested_notional_usd=notional,
        strategy_id='ema_cross_v1',
        timestamp=datetime.now(timezone.utc),
        allow_risk_resizing=True,
    )


def make_market(price=30000.0):
    return MarketDataSnapshot(symbol='BTC-USD', price=price, timestamp=datetime.now(timezone.utc))


def make_portfolio():
    p = Portfolio(starting_cash=100000.0)
    return p.snapshot(30000.0)


def test_buy_commits_once():
    store = InMemoryTradeEventStore()
    # initialize risk state for portfolio
    init_state = {'portfolio_id': 'default', 'current_equity': 100000.0, 'peak_equity': 100000.0, 'day_start_equity': 100000.0, 'trading_day_utc': datetime.now(timezone.utc).date().isoformat(), 'daily_pnl': 0.0, 'current_drawdown': 0.0, 'updated_at': datetime.now(timezone.utc).isoformat()}
    store.initialize_portfolio_risk_state('default', init_state)
    portfolio = Portfolio(starting_cash=100000.0)
    broker = PaperBroker(portfolio)
    from src.risk.risk_engine import RiskConfig
    rc = RiskConfig(starting_capital=1000000.0, max_risk_per_trade_pct=1.0, daily_loss_cutoff=0.99, max_drawdown_cutoff=0.99)
    svc = AgentTradeService(broker, store=store, risk_config=rc)

    proposal = make_proposal(action='BUY', notional=100.0)
    market = make_market(30000.0)
    portfolio_snapshot = make_portfolio()
    req = PaperOrderRequest(proposal=proposal, market_snapshot=market, portfolio_snapshot=portfolio_snapshot)

    res = svc.submit_paper_order(req)
    assert res.filled_quantity > 0
    # second call with same idempotency should return same result
    req2 = PaperOrderRequest(proposal=proposal, market_snapshot=market, portfolio_snapshot=portfolio_snapshot)
    res2 = svc.submit_paper_order(req2)
    assert res2.fill_id == res.fill_id


def test_conflicting_idempotency_rejected():
    store = InMemoryTradeEventStore()
    init_state = {'portfolio_id': 'default', 'current_equity': 100000.0, 'peak_equity': 100000.0, 'day_start_equity': 100000.0, 'trading_day_utc': datetime.now(timezone.utc).date().isoformat(), 'daily_pnl': 0.0, 'current_drawdown': 0.0, 'updated_at': datetime.now(timezone.utc).isoformat()}
    store.initialize_portfolio_risk_state('default', init_state)
    portfolio = Portfolio(starting_cash=100000.0)
    broker = PaperBroker(portfolio)
    from src.risk.risk_engine import RiskConfig
    rc = RiskConfig(starting_capital=1000000.0, max_risk_per_trade_pct=1.0, daily_loss_cutoff=0.99, max_drawdown_cutoff=0.99)
    svc = AgentTradeService(broker, store=store, risk_config=rc)

    # create two proposals with same idempotency key but different notional
    shared_key = str(uuid.uuid4())
    p1 = make_proposal()
    p1.idempotency_key = shared_key
    p2 = make_proposal()
    p2.idempotency_key = shared_key
    p2.requested_notional_usd = 2000.0

    market = make_market()
    req1 = PaperOrderRequest(proposal=p1, market_snapshot=market, portfolio_snapshot=make_portfolio())
    res1 = svc.submit_paper_order(req1)
    # second conflicting request should raise
    req2 = PaperOrderRequest(proposal=p2, market_snapshot=market, portfolio_snapshot=make_portfolio())
    try:
        svc.submit_paper_order(req2)
        assert False, 'Expected PaperOrderRejected'
    except PaperOrderRejected:
        pass
