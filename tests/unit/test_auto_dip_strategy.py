from datetime import datetime, timedelta, timezone
import json
import logging

import pytest

from src.agent.models import PortfolioRiskState
from src.agent.store import InMemoryTradeEventStore
from src.broker.paper_broker import PaperBroker
from src.models.schemas import MarketDataResponse, OHLCV
from src.portfolio.portfolio import Portfolio
from src.strategies.dip_buy import DipBuyConfig, DipBuyStrategy, STRATEGY_STATE_KEY


NOW = datetime.now(timezone.utc)


def market(reference: float, current: float) -> MarketDataResponse:
    prices = [reference, reference, reference, current]
    candles = [
        OHLCV(
            timestamp=NOW - timedelta(minutes=45 - index * 15),
            open=price, high=price, low=price, close=price, volume=1.0,
        )
        for index, price in enumerate(prices)
    ]
    return MarketDataResponse(
        symbol='BTC-USD', timeframe='15m', timestamps=[item.timestamp for item in candles],
        ohlcv=candles, latest_price=current, source='deterministic-test', retrieved_at=NOW,
    )


def strategy(response, *, store=None, portfolio=None, config=None):
    store = store or InMemoryTradeEventStore()
    portfolio = portfolio or Portfolio()
    return DipBuyStrategy(
        store=store, broker=PaperBroker(portfolio), config=config or DipBuyConfig(),
        market_loader=lambda *_: response, now_fn=lambda: NOW,
    ), store


def initialize_risk(store, portfolio, price):
    snapshot = portfolio.snapshot(price)
    state = PortfolioRiskState(
        portfolio_id='default', current_equity=snapshot['total_equity'],
        peak_equity=snapshot['total_equity'], day_start_equity=snapshot['total_equity'],
        trading_day_utc=NOW.date().isoformat(), daily_pnl=0.0, current_drawdown=0.0,
        updated_at=NOW, version=1,
    )
    store.initialize_portfolio_risk_state('default', state.model_dump())


@pytest.mark.parametrize(
    ('current', 'expected_signal', 'expected_executed'),
    [(98.1, False, False), (98.0, True, True), (97.0, True, True)],
)
def test_threshold_boundary(current, expected_signal, expected_executed):
    subject, _ = strategy(market(100.0, current))
    result = subject.evaluate_and_maybe_execute()
    assert result['signal'] is expected_signal
    assert result['executed'] is expected_executed


def test_active_signal_is_not_repeated():
    subject, store = strategy(market(100.0, 98.0), config=DipBuyConfig(cooldown_minutes=0))
    first = subject.evaluate_and_maybe_execute()
    second = subject.evaluate_and_maybe_execute()
    assert first['result'] == 'PAPER_ORDER_COMMITTED'
    assert second['result'] == 'SIGNAL_ALREADY_ACTIVE'
    assert len(store.orders) == 1


def test_cooldown_blocks_rearmed_new_signal():
    subject, store = strategy(market(101.0, 98.0))
    store.save_strategy_state(STRATEGY_STATE_KEY, {
        'signal_active': False, 'last_signal_id': 'older-signal',
        'last_auto_buy_at': (NOW - timedelta(minutes=30)).isoformat(),
    })
    result = subject.evaluate_and_maybe_execute()
    assert result['result'] == 'COOLDOWN_ACTIVE'
    assert not store.orders


def test_same_observation_replay_is_blocked_even_when_rearmed():
    subject, store = strategy(market(100.0, 98.0), config=DipBuyConfig(cooldown_minutes=0))
    first = subject.evaluate_and_maybe_execute()
    saved = store.get_strategy_state(STRATEGY_STATE_KEY)
    saved['signal_active'] = False
    store.save_strategy_state(STRATEGY_STATE_KEY, saved)
    second = subject.evaluate_and_maybe_execute()
    assert first['executed'] is True
    assert second['result'] == 'SIGNAL_REPLAY'
    assert len(store.orders) == 1


def test_max_position_rule_rejects_automatic_buy():
    store = InMemoryTradeEventStore()
    portfolio = Portfolio(cash=9900.0, btc_quantity=1.0, avg_entry_price=100.0)
    initialize_risk(store, portfolio, 98.0)
    subject, _ = strategy(market(100.0, 98.0), store=store, portfolio=portfolio)
    result = subject.evaluate_and_maybe_execute()
    assert result['rejection_reason'] == 'MAX_POSITIONS_EXCEEDED'
    assert not store.orders


def test_insufficient_paper_cash_is_rejected():
    store = InMemoryTradeEventStore()
    portfolio = Portfolio(starting_cash=10.0, cash=10.0)
    initialize_risk(store, portfolio, 98.0)
    subject, _ = strategy(market(100.0, 98.0), store=store, portfolio=portfolio)
    result = subject.evaluate_and_maybe_execute()
    assert result['rejection_reason'] == 'INSUFFICIENT_PAPER_BALANCE'
    assert not store.orders


def test_wrong_symbol_fails_before_market_access():
    called = False
    def forbidden_loader(*_):
        nonlocal called
        called = True
        raise AssertionError('market loader must not run')
    subject = DipBuyStrategy(
        InMemoryTradeEventStore(), PaperBroker(Portfolio()), DipBuyConfig(symbol='ETH-USD'),
        market_loader=forbidden_loader, now_fn=lambda: NOW,
    )
    result = subject.evaluate_and_maybe_execute()
    assert result['rejection_reason'] == 'INVALID_ASSET'
    assert called is False


def test_disabled_strategy_does_not_fetch_market_or_trade():
    subject = DipBuyStrategy(
        InMemoryTradeEventStore(), PaperBroker(Portfolio()), DipBuyConfig(enabled=False),
        market_loader=lambda *_: pytest.fail('market loader must not run'), now_fn=lambda: NOW,
    )
    result = subject.evaluate_and_maybe_execute()
    assert result['result'] == 'DISABLED'
    assert result['executed'] is False


def test_success_uses_existing_atomic_paper_order_path():
    subject, store = strategy(market(100.0, 98.0))
    result = subject.evaluate_and_maybe_execute()
    assert result['executed'] is True
    assert len(store.orders) == len(store.fills) == 1
    state = store.get_strategy_state(STRATEGY_STATE_KEY)
    assert state['last_trade_id'] == result['paper_trade_id']
    assert state['last_result'] == 'PAPER_ORDER_COMMITTED'
    assert len(state['positions']) == 1
    assert state['positions'][0]['entry_order_id'] == result['paper_trade_id']
    assert state['positions'][0]['quantity_remaining'] > 0
    assert state['positions'][0]['exit_state'] == 'OPEN'


def test_paper_broker_has_no_public_live_order_method():
    with pytest.raises(RuntimeError, match='disabled'):
        PaperBroker(Portfolio()).submit_order()


def test_every_evaluation_emits_structured_log(caplog):
    subject, _ = strategy(market(100.0, 98.1))
    with caplog.at_level(logging.INFO, logger='src.strategies.dip_buy'):
        subject.evaluate_and_maybe_execute()
    record = json.loads(caplog.records[-1].message)
    assert record['event'] == 'dip_buy_evaluation'
    assert record['symbol'] == 'BTC-USD'
    assert record['current_price'] == 98.1
    assert record['reference_price'] == 100.0
    assert record['signal'] is False
    assert record['result'] == 'NO_DIP'
