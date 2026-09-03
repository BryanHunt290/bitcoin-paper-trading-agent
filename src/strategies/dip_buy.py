from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..agent.exceptions import PaperOrderRejected
from ..agent.models import AgentTradeProposal, MarketDataSnapshot, PaperOrderRequest
from ..agent.store import TradeEventStore
from ..agent.tools import AgentTradeService
from ..broker.paper_broker import PaperBroker
from ..market_data.adapter import get_market_data
from ..models.schemas import MarketDataResponse


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

STRATEGY_ID = 'dip_buy_v1'
STRATEGY_STATE_KEY = 'DIP_BUY_V1'
CANDLE_MINUTES = 15


@dataclass(frozen=True)
class DipBuyConfig:
    dip_threshold_pct: float = 0.02
    lookback_minutes: int = 60
    paper_order_usd: float = 25.0
    cooldown_minutes: int = 60
    enabled: bool = True
    symbol: str = 'BTC-USD'
    take_profit_pct: float = 0.03
    stop_loss_pct: float = 0.02
    trailing_stop_enabled: bool = True
    trailing_stop_pct: float = 0.015

    def __post_init__(self) -> None:
        if not math.isfinite(self.dip_threshold_pct) or not 0 < self.dip_threshold_pct < 1:
            raise ValueError('dip_threshold_pct must be between 0 and 1')
        if not 15 <= self.lookback_minutes <= 24 * 60:
            raise ValueError('lookback_minutes must be between 15 and 1440')
        if not math.isfinite(self.paper_order_usd) or self.paper_order_usd <= 0:
            raise ValueError('paper_order_usd must be positive')
        if not 0 <= self.cooldown_minutes <= 7 * 24 * 60:
            raise ValueError('cooldown_minutes must be between 0 and 10080')
        for name in ('take_profit_pct', 'stop_loss_pct', 'trailing_stop_pct'):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f'{name} must be between 0 and 1')


class DipBuyStrategy:
    """Deterministic BTC-USD paper entry and exit strategy."""

    def __init__(
        self,
        store: TradeEventStore,
        broker: PaperBroker,
        config: DipBuyConfig | None = None,
        *,
        market_loader: Callable[[str, str, int], MarketDataResponse] = get_market_data,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.config = config or DipBuyConfig()
        self.market_loader = market_loader
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _log(result: dict[str, Any]) -> None:
        logger.info(json.dumps({'event': 'dip_buy_evaluation', **result}, default=str, sort_keys=True))

    def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        self._log(result)
        return result

    def _market(self, now: datetime) -> tuple[float, float, str, str]:
        count = min(300, math.ceil(self.config.lookback_minutes / CANDLE_MINUTES) + 1)
        response = self.market_loader(self.config.symbol, '15m', count)
        cutoff = now - timedelta(minutes=self.config.lookback_minutes)
        candles = [candle for candle in response.ohlcv if candle.timestamp >= cutoff]
        if not candles:
            raise RuntimeError('NO_MARKET_DATA')
        current_price = float(response.latest_price)
        reference_price = max(float(candle.high) for candle in candles)
        if not all(math.isfinite(value) and value > 0 for value in (current_price, reference_price)):
            raise RuntimeError('INVALID_MARKET_DATA')
        latest_candle_at = max(candle.timestamp for candle in candles).astimezone(timezone.utc).isoformat()
        return current_price, reference_price, latest_candle_at, response.source

    def _signal_id(self, latest_candle_at: str, reference_price: float) -> str:
        payload = {
            'strategy_id': STRATEGY_ID,
            'symbol': self.config.symbol,
            'latest_candle_at': latest_candle_at,
            'reference_price': reference_price,
            'threshold_pct': self.config.dip_threshold_pct,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()
        return digest[:32]

    @staticmethod
    def _position_id(position: dict[str, Any]) -> str:
        existing = position.get('position_id')
        if existing:
            return str(existing)
        identity = {
            'entry_order_id': position.get('entry_order_id'),
            'opened_at': position.get('opened_at'),
            'entry_price': position.get('entry_price'),
            'quantity': position.get('quantity'),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:32]

    @classmethod
    def _positions(cls, previous: dict[str, Any]) -> list[dict[str, Any]]:
        raw_positions = previous.get('positions', [])
        if not isinstance(raw_positions, list):
            raise ValueError('positions must be a list')
        positions = copy.deepcopy(raw_positions)
        for position in positions:
            if not isinstance(position, dict):
                raise ValueError('position must be an object')
            entry_price = float(position.get('entry_price', 0.0))
            quantity = float(position.get('quantity', 0.0))
            remaining = float(position.get('quantity_remaining', quantity))
            highest = float(position.get('highest_price_since_entry', entry_price))
            if not all(math.isfinite(value) for value in (entry_price, quantity, remaining, highest)):
                raise ValueError('position values must be finite')
            if entry_price <= 0 or quantity <= 0 or remaining < 0 or remaining > quantity or highest <= 0:
                raise ValueError('position values are invalid')
            position.update({
                'position_id': cls._position_id(position),
                'entry_price': entry_price,
                'quantity': quantity,
                'quantity_remaining': remaining,
                'highest_price_since_entry': max(highest, entry_price),
            })
        return positions

    def _save_state(self, state: dict[str, Any]) -> None:
        self.store.save_strategy_state(STRATEGY_STATE_KEY, state)

    def _request(self, proposal: AgentTradeProposal, current_price: float, now: datetime) -> PaperOrderRequest:
        portfolio_snapshot = self.broker.portfolio.snapshot(current_price)
        portfolio_snapshot['portfolio_id'] = 'default'
        return PaperOrderRequest(
            proposal=proposal,
            portfolio_snapshot=portfolio_snapshot,
            market_snapshot=MarketDataSnapshot(symbol='BTC-USD', price=current_price, timestamp=now),
        )

    def _exit_reason(self, position: dict[str, Any], current_price: float) -> str | None:
        cfg = self.config
        entry_price = position['entry_price']
        position['highest_price_since_entry'] = max(
            position['highest_price_since_entry'], current_price
        )
        if current_price >= entry_price * (1.0 + cfg.take_profit_pct):
            return 'TAKE_PROFIT'
        if current_price <= entry_price * (1.0 - cfg.stop_loss_pct):
            return 'STOP_LOSS'
        highest = position['highest_price_since_entry']
        if (
            cfg.trailing_stop_enabled
            and highest > entry_price
            and current_price <= highest * (1.0 - cfg.trailing_stop_pct)
        ):
            return 'TRAILING_STOP'
        return None

    def _execute_exit(
        self,
        *,
        state: dict[str, Any],
        position_index: int,
        exit_reason: str,
        current_price: float,
        now: datetime,
        evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        position = state['positions'][position_index]
        position_id = position['position_id']
        idempotency_key = f'AUTO#EXIT#{STRATEGY_ID}#{position_id}#{exit_reason}'
        proposal = AgentTradeProposal(
            symbol='BTC-USD',
            action='SELL',
            strategy_id=STRATEGY_ID,
            requested_quantity=position['quantity_remaining'],
            timestamp=now,
            idempotency_key=idempotency_key,
            execution_mode='PAPER',
            allow_risk_resizing=False,
        )
        request = self._request(proposal, current_price, now)

        def completed_state(candidate: dict) -> dict:
            committed = copy.deepcopy(state)
            completed = committed['positions'][position_index]
            filled_quantity = min(
                float(candidate['filled_quantity']), completed['quantity_remaining']
            )
            filled_price = float(candidate['filled_price'])
            fees = float(candidate['fees'])
            remaining = max(0.0, completed['quantity_remaining'] - filled_quantity)
            completed.update({
                'quantity_remaining': remaining,
                'exit_state': 'COMPLETED' if remaining == 0 else 'PARTIAL',
                'exit_reason': exit_reason,
                'exit_requested_at': now.isoformat(),
                'exit_order_id': candidate['paper_order_id'],
                'exit_fill_id': candidate['fill_id'],
                'exit_filled_quantity': filled_quantity,
                'exit_filled_price': filled_price,
                'exit_fees': fees,
                'exit_realized_pnl': filled_quantity * (filled_price - completed['entry_price']) - fees,
                'closed_at': now.isoformat() if remaining == 0 else None,
                'exit_idempotency_key': idempotency_key,
            })
            committed.update({
                'last_result': 'PAPER_EXIT_COMMITTED',
                'last_exit_at': now.isoformat(),
                'last_rejection_reason': None,
            })
            return committed

        try:
            order = AgentTradeService(broker=self.broker, store=self.store).submit_paper_order(
                request,
                strategy_state_name=STRATEGY_STATE_KEY,
                strategy_state_builder=completed_state,
            )
        except PaperOrderRejected as exc:
            reason = exc.details.reason_code
            position.update({
                'exit_state': 'REJECTED',
                'last_exit_reason': exit_reason,
                'last_rejection': reason,
                'exit_idempotency_key': idempotency_key,
            })
            state.update({'last_result': 'EXIT_REJECTED', 'last_rejection_reason': reason})
            self._save_state(state)
            return self._finish({
                **evaluation,
                'executed': False,
                'result': 'EXIT_REJECTED',
                'exit_reason': exit_reason,
                'rejection_reason': reason,
            })

        return self._finish({
            **evaluation,
            'executed': True,
            'result': 'PAPER_EXIT_COMMITTED',
            'exit_reason': exit_reason,
            'paper_trade_id': order.paper_order_id,
            'fill_id': order.fill_id,
        })

    def evaluate_and_maybe_execute(self) -> dict[str, Any]:
        now = self.now_fn().astimezone(timezone.utc)
        cfg = self.config
        base = {
            'symbol': cfg.symbol,
            'enabled': cfg.enabled,
            'threshold_pct': cfg.dip_threshold_pct,
            'lookback_minutes': cfg.lookback_minutes,
            'order_usd': cfg.paper_order_usd,
            'cooldown_minutes': cfg.cooldown_minutes,
            'take_profit_pct': cfg.take_profit_pct,
            'stop_loss_pct': cfg.stop_loss_pct,
            'trailing_stop_enabled': cfg.trailing_stop_enabled,
            'trailing_stop_pct': cfg.trailing_stop_pct,
            'last_evaluated_at': now.isoformat(),
        }

        if not cfg.enabled:
            result = {**base, 'signal': False, 'executed': False, 'result': 'DISABLED'}
            return self._finish(result)
        if cfg.symbol != 'BTC-USD':
            result = {
                **base, 'signal': False, 'executed': False, 'result': 'REJECTED',
                'rejection_reason': 'INVALID_ASSET',
            }
            return self._finish(result)

        try:
            previous = self.store.get_strategy_state(STRATEGY_STATE_KEY) or {}
        except Exception:
            return self._finish({
                **base, 'signal': False, 'executed': False, 'result': 'STATE_UNAVAILABLE',
                'rejection_reason': 'STRATEGY_STATE_UNAVAILABLE',
            })
        try:
            positions = self._positions(previous)
        except (TypeError, ValueError):
            return self._finish({
                **base, 'signal': False, 'executed': False, 'result': 'STATE_INVALID',
                'rejection_reason': 'INVALID_POSITION_STATE',
            })

        try:
            current_price, reference_price, latest_candle_at, source = self._market(now)
        except Exception as exc:
            return self._finish({
                **base, 'signal': False, 'executed': False, 'result': 'MARKET_UNAVAILABLE',
                'rejection_reason': type(exc).__name__,
            })

        dip_pct = max(0.0, (reference_price - current_price) / reference_price)
        signal = dip_pct >= cfg.dip_threshold_pct
        signal_id = self._signal_id(latest_candle_at, reference_price)
        evaluation = {
            **base,
            'current_price': current_price,
            'reference_price': reference_price,
            'dip_pct': dip_pct,
            'signal': signal,
            'signal_id': signal_id,
            'market_source': source,
        }
        state = {
            **copy.deepcopy(previous),
            **evaluation,
            'positions': positions,
            'last_signal_id': signal_id if signal else previous.get('last_signal_id'),
        }

        for index, position in enumerate(positions):
            if position['quantity_remaining'] <= 0:
                continue
            exit_reason = self._exit_reason(position, current_price)
            if exit_reason:
                return self._execute_exit(
                    state=state,
                    position_index=index,
                    exit_reason=exit_reason,
                    current_price=current_price,
                    now=now,
                    evaluation=evaluation,
                )

        if not signal:
            result = {**evaluation, 'executed': False, 'result': 'NO_DIP'}
            self._save_state({**state, **result, 'signal_active': False})
            return self._finish(result)

        if previous.get('signal_active'):
            result = {**evaluation, 'executed': False, 'result': 'SIGNAL_ALREADY_ACTIVE'}
            self._save_state({**state, **result, 'signal_active': True})
            return self._finish(result)

        if previous.get('last_signal_id') == signal_id:
            result = {**evaluation, 'executed': False, 'result': 'SIGNAL_REPLAY'}
            self._save_state({**state, **result, 'signal_active': True})
            return self._finish(result)

        last_auto_buy_at = previous.get('last_auto_buy_at')
        if last_auto_buy_at:
            try:
                last_buy = datetime.fromisoformat(str(last_auto_buy_at).replace('Z', '+00:00'))
                if last_buy.tzinfo is None:
                    raise ValueError('last_auto_buy_at must be timezone-aware')
            except (TypeError, ValueError):
                result = {
                    **evaluation, 'executed': False, 'result': 'STATE_INVALID',
                    'rejection_reason': 'INVALID_LAST_AUTO_BUY_AT',
                }
                self._save_state({**state, **result, 'signal_active': True})
                return self._finish(result)
            if now - last_buy.astimezone(timezone.utc) < timedelta(minutes=cfg.cooldown_minutes):
                result = {**evaluation, 'executed': False, 'result': 'COOLDOWN_ACTIVE'}
                self._save_state({**state, **result, 'signal_active': True})
                return self._finish(result)

        idempotency_key = f'AUTO#{STRATEGY_ID}#{signal_id}'
        proposal = AgentTradeProposal(
            symbol='BTC-USD',
            action='BUY',
            strategy_id=STRATEGY_ID,
            requested_notional_usd=cfg.paper_order_usd,
            timestamp=now,
            idempotency_key=idempotency_key,
            execution_mode='PAPER',
            allow_risk_resizing=False,
        )
        request = self._request(proposal, current_price, now)
        pending_state = {
            **state,
            'signal_active': True,
            'last_auto_buy_at': now.isoformat(),
            'last_result': 'PAPER_ORDER_COMMITTED',
            'last_rejection_reason': None,
        }

        def opened_state(candidate: dict) -> dict:
            committed = copy.deepcopy(pending_state)
            committed['positions'].append({
                'position_id': signal_id,
                'entry_order_id': candidate['paper_order_id'],
                'entry_fill_id': candidate['fill_id'],
                'entry_idempotency_key': idempotency_key,
                'entry_price': float(candidate['filled_price']),
                'quantity': float(candidate['filled_quantity']),
                'quantity_remaining': float(candidate['filled_quantity']),
                'entry_fees': float(candidate['fees']),
                'opened_at': now.isoformat(),
                'highest_price_since_entry': float(candidate['filled_price']),
                'exit_state': 'OPEN',
            })
            return committed

        try:
            order = AgentTradeService(broker=self.broker, store=self.store).submit_paper_order(
                request,
                strategy_state_name=STRATEGY_STATE_KEY,
                strategy_state_builder=opened_state,
            )
        except PaperOrderRejected as exc:
            reason = exc.details.reason_code
            result = {
                **evaluation, 'executed': False, 'result': 'REJECTED',
                'rejection_reason': reason,
            }
            self._save_state({
                **state, **result, 'signal_active': True,
                'last_result': 'REJECTED', 'last_rejection_reason': reason,
            })
            return self._finish(result)

        return self._finish({
            **evaluation,
            'executed': True,
            'result': 'PAPER_ORDER_COMMITTED',
            'paper_trade_id': order.paper_order_id,
            'fill_id': order.fill_id,
        })
