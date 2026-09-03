import pytest
from datetime import datetime, timezone, timedelta, date

from src.agent.store import InMemoryTradeEventStore
from src.agent.models import PortfolioRiskState


def test_initialize_and_version_conflict():
    store = InMemoryTradeEventStore()
    pid = 'default'
    now = datetime.now(timezone.utc)
    state = PortfolioRiskState(
        portfolio_id=pid,
        current_equity=10000.0,
        peak_equity=10000.0,
        day_start_equity=10000.0,
        trading_day_utc=now.date().isoformat(),
        daily_pnl=0.0,
        current_drawdown=0.0,
        updated_at=now,
        version=1,
    )
    res = store.initialize_portfolio_risk_state(pid, state.model_dump())
    assert res['version'] == 1
    # conflict on second initialize
    res2 = store.initialize_portfolio_risk_state(pid, state.model_dump())
    assert res2.get('conflict')


def test_versioned_save_and_conflict():
    store = InMemoryTradeEventStore()
    pid = 'p1'
    now = datetime.now(timezone.utc)
    state = PortfolioRiskState(
        portfolio_id=pid,
        current_equity=10000.0,
        peak_equity=10000.0,
        day_start_equity=10000.0,
        trading_day_utc=now.date().isoformat(),
        daily_pnl=0.0,
        current_drawdown=0.0,
        updated_at=now,
        version=1,
    )
    init = store.initialize_portfolio_risk_state(pid, state.model_dump())
    assert init['version'] == 1
    # load and modify then save with expected_version
    s = store.get_portfolio_risk_state(pid)
    assert s['version'] == 1
    new_state = s['state']
    new_state['current_equity'] = 11000.0
    r = store.save_portfolio_risk_state(pid, new_state, expected_version=1)
    assert r['version'] == 2
    # attempt stale write
    stale = dict(new_state)
    stale['current_equity'] = 9000.0
    conflict = store.save_portfolio_risk_state(pid, stale, expected_version=1)
    assert conflict.get('conflict')


def test_peak_drawdown_persistence_and_restart_behavior():
    store = InMemoryTradeEventStore()
    pid = 'restart1'
    now = datetime.now(timezone.utc)
    state = PortfolioRiskState(
        portfolio_id=pid,
        current_equity=10000.0,
        peak_equity=12000.0,
        day_start_equity=10000.0,
        trading_day_utc=now.date().isoformat(),
        daily_pnl=0.0,
        current_drawdown=(12000.0-10000.0)/12000.0,
        updated_at=now,
        version=1,
    )
    store.initialize_portfolio_risk_state(pid, state.model_dump())
    # simulate equity drop below drawdown cutoff threshold
    s = store.get_portfolio_risk_state(pid)
    modified = s['state']
    modified['current_equity'] = 7000.0
    # save with correct version
    r = store.save_portfolio_risk_state(pid, modified, expected_version=s['version'])
    assert r['version'] == 2
    # reload to mimic restart
    reloaded = store.get_portfolio_risk_state(pid)
    assert reloaded['state']['current_equity'] == 7000.0
    assert reloaded['state']['peak_equity'] == 12000.0
