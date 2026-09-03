from datetime import datetime, timezone
from src.risk.risk_engine import calculate_position_size, RiskConfig
from src.portfolio.portfolio import Portfolio
from src.broker.paper_broker import PaperBroker


def test_risk_rejects_large_notional():
    p = Portfolio()
    state = p.snapshot(20000)
    state['latest_price'] = 20000
    cfg = RiskConfig()
    # request more than allowed by 0.5% of 10k -> 50 USD
    r = calculate_position_size(1000.0, state, cfg)
    assert not r['allowed']


def test_paper_broker_buy_and_sell():
    p = Portfolio()
    broker = PaperBroker(p)
    ts = datetime.now(timezone.utc)
    # buy 0.1 BTC at price 20000
    evt = broker._execute_order('BUY', 0.1, 20000, 'test', ts)
    assert 'order_id' in evt
    assert p.btc_quantity > 0
    # sell it back
    evt2 = broker._execute_order('SELL', p.btc_quantity, 20010, 'test', ts)
    assert p.btc_quantity == 0
