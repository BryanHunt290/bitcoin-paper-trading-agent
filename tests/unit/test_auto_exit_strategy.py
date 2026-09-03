from datetime import datetime, timedelta, timezone

import pytest

from src.agent.models import PortfolioRiskState
from src.agent.store import InMemoryTradeEventStore
from src.broker.paper_broker import PaperBroker
from src.models.schemas import MarketDataResponse, OHLCV
from src.portfolio.portfolio import Portfolio
from src.strategies.dip_buy import DipBuyConfig, DipBuyStrategy, STRATEGY_STATE_KEY


NOW = datetime.now(timezone.utc)


def market(price: float, high: float | None = None) -> MarketDataResponse:
    high = price if high is None else high
    candles = [
        OHLCV(
            timestamp=NOW - timedelta(minutes=30 - index * 15),
            open=price, high=high, low=price, close=price, volume=1.0,
        )
        for index in range(3)
    ]
    return MarketDataResponse(
        symbol='BTC-USD', timeframe='15m', timestamps=[item.timestamp for item in candles],
        ohlcv=candles, latest_price=price, source='deterministic-test', retrieved_at=NOW,
    )


def initialize_risk(store: InMemoryTradeEventStore, portfolio: Portfolio, price: float) -> None:
    equity = portfolio.snapshot(price)['total_equity']
    state = PortfolioRiskState(
        portfolio_id='default', current_equity=equity, peak_equity=equity,
        day_start_equity=equity, trading_day_utc=NOW.date().isoformat(),
        daily_pnl=0.0, current_drawdown=0.0, updated_at=NOW, version=1,
    )
    store.initialize_portfolio_risk_state('default', state.model_dump())


def exit_strategy(
    price: float,
    *,
    config: DipBuyConfig | None = None,
    highest: float = 100.0,
    portfolio_quantity: float = 1.0,
) -> tuple[DipBuyStrategy, InMemoryTradeEventStore, Portfolio]:
    store = InMemoryTradeEventStore()
    portfolio = Portfolio(cash=9900.0, btc_quantity=portfolio_quantity, avg_entry_price=100.0)
    initialize_risk(store, portfolio, price)
    store.save_strategy_state(STRATEGY_STATE_KEY, {
        'positions': [{
            'position_id': 'position-1',
            'entry_order_id': 'ORDER#entry',
            'entry_fill_id': 'FILL#entry',
            'entry_idempotency_key': 'AUTO#entry',
            'entry_price': 100.0,
            'quantity': 1.0,
            'quantity_remaining': 1.0,
            'opened_at': (NOW - timedelta(hours=1)).isoformat(),
            'highest_price_since_entry': highest,
            'exit_state': 'OPEN',
        }],
    })
    response = market(price)
    subject = DipBuyStrategy(
        store=store,
        broker=PaperBroker(portfolio),
        config=config or DipBuyConfig(trailing_stop_enabled=False),
        market_loader=lambda *_: response,
        now_fn=lambda: NOW,
    )
    return subject, store, portfolio


def test_exact_take_profit_commits_paper_sell_and_position_state_atomically():
    subject, store, portfolio = exit_strategy(103.0)
    result = subject.evaluate_and_maybe_execute()
    state = store.get_strategy_state(STRATEGY_STATE_KEY)
    position = state['positions'][0]

    assert result['result'] == 'PAPER_EXIT_COMMITTED'
    assert result['exit_reason'] == 'TAKE_PROFIT'
    assert len(store.orders) == len(store.fills) == 1
    assert portfolio.btc_quantity == 0.0
    assert position['quantity_remaining'] == 0.0
    assert position['exit_state'] == 'COMPLETED'
    assert position['exit_order_id'] == result['paper_trade_id']
    assert position['exit_fill_id'] == result['fill_id']
    assert position['exit_idempotency_key'].startswith('AUTO#EXIT#dip_buy_v1#')
    assert position['exit_realized_pnl'] > 0
    assert state['last_trade_id'] == result['paper_trade_id']


def test_exact_stop_loss_commits_paper_sell():
    subject, store, _ = exit_strategy(98.0)
    result = subject.evaluate_and_maybe_execute()
    position = store.get_strategy_state(STRATEGY_STATE_KEY)['positions'][0]
    assert result['result'] == 'PAPER_EXIT_COMMITTED'
    assert result['exit_reason'] == 'STOP_LOSS'
    assert position['exit_realized_pnl'] < 0


def test_trailing_stop_uses_persisted_high_water_mark():
    config = DipBuyConfig(
        take_profit_pct=0.20,
        stop_loss_pct=0.20,
        trailing_stop_enabled=True,
        trailing_stop_pct=0.015,
    )
    subject, store, _ = exit_strategy(108.35, config=config, highest=110.0)
    result = subject.evaluate_and_maybe_execute()
    position = store.get_strategy_state(STRATEGY_STATE_KEY)['positions'][0]
    assert result['exit_reason'] == 'TRAILING_STOP'
    assert position['highest_price_since_entry'] == 110.0


def test_open_position_updates_high_without_exit():
    subject, store, _ = exit_strategy(101.0, config=DipBuyConfig())
    result = subject.evaluate_and_maybe_execute()
    position = store.get_strategy_state(STRATEGY_STATE_KEY)['positions'][0]
    assert result['result'] == 'NO_DIP'
    assert not store.orders
    assert position['highest_price_since_entry'] == 101.0
    assert position['exit_state'] == 'OPEN'


def test_completed_exit_is_not_replayed():
    subject, store, _ = exit_strategy(103.0)
    first = subject.evaluate_and_maybe_execute()
    second = subject.evaluate_and_maybe_execute()
    assert first['result'] == 'PAPER_EXIT_COMMITTED'
    assert second['result'] == 'NO_DIP'
    assert len(store.orders) == 1


def test_exit_rejection_is_recorded_without_closing_position():
    subject, store, portfolio = exit_strategy(103.0, portfolio_quantity=0.5)
    result = subject.evaluate_and_maybe_execute()
    position = store.get_strategy_state(STRATEGY_STATE_KEY)['positions'][0]
    assert result['result'] == 'EXIT_REJECTED'
    assert result['rejection_reason'] == 'INVALID_QUANTITY'
    assert position['quantity_remaining'] == 1.0
    assert position['exit_state'] == 'REJECTED'
    assert portfolio.btc_quantity == 0.5
    assert not store.orders


def test_disabled_strategy_does_not_exit_or_destroy_position_state():
    subject, store, portfolio = exit_strategy(103.0, config=DipBuyConfig(enabled=False))
    before = store.get_strategy_state(STRATEGY_STATE_KEY)
    result = subject.evaluate_and_maybe_execute()
    assert result['result'] == 'DISABLED'
    assert store.get_strategy_state(STRATEGY_STATE_KEY) == before
    assert portfolio.btc_quantity == 1.0


@pytest.mark.parametrize(
    ('field', 'value'),
    [('take_profit_pct', 0), ('stop_loss_pct', 1), ('trailing_stop_pct', float('nan'))],
)
def test_invalid_exit_configuration_fails_closed(field, value):
    with pytest.raises(ValueError, match=field):
        DipBuyConfig(**{field: value})
