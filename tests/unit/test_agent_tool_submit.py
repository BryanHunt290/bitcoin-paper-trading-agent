from datetime import datetime, timedelta, timezone
from src.agent.models import AgentTradeProposal, MarketDataSnapshot, PaperOrderRequest, RejectionReason
from src.agent.tools import AgentTradeService
from src.agent.store import InMemoryTradeEventStore
from src.agent.exceptions import PaperOrderRejected
from src.broker.paper_broker import PaperBroker
from src.portfolio.portfolio import Portfolio
import pytest


def make_portfolio_with_cash(cash=10000.0, btc_qty=0.0, price=20000.0):
    p = Portfolio(starting_cash=cash, cash=cash, btc_quantity=btc_qty, avg_entry_price=price)
    return p


def test_reject_eth_request():
    p = make_portfolio_with_cash()
    broker = PaperBroker(p)
    service = AgentTradeService(broker, InMemoryTradeEventStore())
    proposal = AgentTradeProposal(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', requested_notional_usd=100.0, timestamp=datetime.now(timezone.utc))
    market = MarketDataSnapshot(symbol='BTC-USD', price=20000.0, timestamp=datetime.now(timezone.utc))
    # ETH test: construct with wrong symbol via raw dict and expect validation elsewhere
    with pytest.raises(Exception):
        AgentTradeProposal(symbol='ETH-USD', action='BUY', strategy_id='ema_cross_v1', requested_notional_usd=100.0, timestamp=datetime.now(timezone.utc))


def test_reject_leverage():
    p = make_portfolio_with_cash()
    broker = PaperBroker(p)
    service = AgentTradeService(broker, InMemoryTradeEventStore())
    proposal = AgentTradeProposal(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', requested_notional_usd=20000.0, timestamp=datetime.now(timezone.utc), allow_risk_resizing=False)
    market = MarketDataSnapshot(symbol='BTC-USD', price=20000.0, timestamp=datetime.now(timezone.utc))
    req = PaperOrderRequest(proposal=proposal, portfolio_snapshot=p.snapshot(market.price), market_snapshot=market)
    with pytest.raises(PaperOrderRejected) as exc:
        service.submit_paper_order(req)
    assert 'Risk engine rejected' in exc.value.details.message


def test_valid_buy_passes():
    p = make_portfolio_with_cash()
    broker = PaperBroker(p)
    store = InMemoryTradeEventStore()
    service = AgentTradeService(broker, store)
    proposal = AgentTradeProposal(symbol='BTC-USD', action='BUY', strategy_id='ema_cross_v1', requested_notional_usd=50.0, timestamp=datetime.now(timezone.utc), idempotency_key='k1')
    market = MarketDataSnapshot(symbol='BTC-USD', price=20000.0, timestamp=datetime.now(timezone.utc))
    req = PaperOrderRequest(proposal=proposal, portfolio_snapshot=p.snapshot(market.price), market_snapshot=market)
    res = service.submit_paper_order(req)
    assert res.filled_quantity > 0
    # duplicate submission returns same result
    res2 = service.submit_paper_order(req)
    assert res2.fill_id == res.fill_id


def test_sell_more_than_holdings_rejected():
    p = make_portfolio_with_cash(btc_qty=0.01, price=20000.0)
    broker = PaperBroker(p)
    service = AgentTradeService(broker, InMemoryTradeEventStore())
    proposal = AgentTradeProposal(symbol='BTC-USD', action='SELL', strategy_id='ema_cross_v1', requested_quantity=1.0, timestamp=datetime.now(timezone.utc))
    market = MarketDataSnapshot(symbol='BTC-USD', price=20000.0, timestamp=datetime.now(timezone.utc))
    req = PaperOrderRequest(proposal=proposal, portfolio_snapshot=p.snapshot(market.price), market_snapshot=market)
    with pytest.raises(PaperOrderRejected) as exc:
        service.submit_paper_order(req)
    assert exc.value.details.reason_code == RejectionReason.INVALID_QUANTITY.value or 'quantity exceeds holdings' in exc.value.details.message
