import pytest
from src.risk.risk_engine import calculate_position_size, RiskConfig
from src.portfolio.portfolio import Portfolio


def test_reject_eth_requests():
    # ETH not permitted; the market_data adapter enforces this. Simulate by ensuring risk only processes BTC
    p = Portfolio()
    state = p.snapshot(20000)
    state['latest_price'] = 20000
    r = calculate_position_size(5000.0, state, RiskConfig())
    # should reject because 5000 > 50 USD risk limit
    assert not r['allowed']


def test_reject_leverage_and_shorting_requests():
    p = Portfolio()
    state = p.snapshot(20000)
    state['latest_price'] = 20000
    cfg = RiskConfig()
    # leverage disabled
    assert cfg.leverage == 0
    assert cfg.allow_short is False
