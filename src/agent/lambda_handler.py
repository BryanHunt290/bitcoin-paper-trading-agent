from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from pydantic import ValidationError

from .bedrock_client import BedrockAgentModelProvider
from .config import BedrockConfig
from .dynamo_store import DynamoTradeEventStore
from .exceptions import PaperOrderRejected
from .models import AgentTradeProposal, MarketDataSnapshot, PaperOrderRequest
from .tool_registry import ToolRegistry
from .tools import AgentTradeService
from ..broker.paper_broker import PaperBroker
from ..market_data.adapter import get_market_data
from ..portfolio.portfolio import Portfolio
from ..strategies.dip_buy import DipBuyConfig, DipBuyStrategy


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PORTFOLIO_ID = 'default'
EXPECTED_MODEL_ID = 'amazon.nova-lite-v1:0'
STARTING_CASH_USD = 10_000.0


def _log(event: str, **fields: Any) -> None:
    logger.info(json.dumps({'event': event, **fields}, default=str, sort_keys=True))


def _check_env() -> None:
    required_exact = {
        'PAPER_MODE': 'true',
        'SYMBOL': 'BTC-USD',
        'ALLOW_SYNTHETIC_MARKET_DATA': 'false',
        'BEDROCK_MODEL_ID': EXPECTED_MODEL_ID,
    }
    for key, expected in required_exact.items():
        actual = os.environ.get(key, '')
        matches = actual.lower() == expected if key in ('PAPER_MODE', 'ALLOW_SYNTHETIC_MARKET_DATA') else actual == expected
        if not matches:
            raise RuntimeError(f'Unsafe runtime configuration: {key}')
    if not os.environ.get('TRADE_EVENTS_TABLE', '').strip():
        raise RuntimeError('Unsafe runtime configuration: TRADE_EVENTS_TABLE')
    _dip_config()


def _dip_config() -> DipBuyConfig:
    return DipBuyConfig(
        dip_threshold_pct=float(os.environ.get('DIP_THRESHOLD_PCT', '0.02')),
        lookback_minutes=int(os.environ.get('DIP_LOOKBACK_MINUTES', '60')),
        paper_order_usd=float(os.environ.get('DIP_PAPER_ORDER_USD', '25')),
        cooldown_minutes=int(os.environ.get('DIP_COOLDOWN_MINUTES', '60')),
        enabled=os.environ.get('DIP_BUY_ENABLED', 'true').lower() == 'true',
        symbol='BTC-USD',
    )


def _is_pristine_risk_baseline(risk_state: Dict[str, Any]) -> bool:
    if not isinstance(risk_state, dict) or int(risk_state.get('version', 0)) != 1:
        return False
    state = risk_state.get('state')
    if not isinstance(state, dict) or state.get('portfolio_id') != PORTFOLIO_ID:
        return False
    try:
        return (
            float(state.get('current_equity')) == STARTING_CASH_USD
            and float(state.get('peak_equity')) == STARTING_CASH_USD
            and float(state.get('day_start_equity')) == STARTING_CASH_USD
            and float(state.get('daily_pnl')) == 0.0
            and float(state.get('current_drawdown')) == 0.0
        )
    except (TypeError, ValueError):
        return False


def _load_state() -> Tuple[DynamoTradeEventStore, Portfolio]:
    store = DynamoTradeEventStore(
        table_name=os.environ['TRADE_EVENTS_TABLE'],
        region_name=os.environ.get('AWS_REGION'),
    )
    risk_state = store.get_portfolio_risk_state(PORTFOLIO_ID)
    snapshot = store.get_latest_portfolio_snapshot(PORTFOLIO_ID)
    if snapshot is None:
        if risk_state is not None and not _is_pristine_risk_baseline(risk_state):
            raise RuntimeError('PORTFOLIO_SNAPSHOT_UNAVAILABLE')
        portfolio = Portfolio(starting_cash=STARTING_CASH_USD, cash=STARTING_CASH_USD)
    else:
        portfolio = Portfolio.from_snapshot(snapshot)
    return store, portfolio


def _fresh_market_snapshot() -> Tuple[MarketDataSnapshot, Dict[str, Any]]:
    response = get_market_data('BTC-USD', '15m', 20)
    snapshot = MarketDataSnapshot(
        symbol='BTC-USD',
        price=response.latest_price,
        timestamp=response.retrieved_at,
    )
    latest_candle = response.timestamps[-1]
    metadata = {
        'source': response.source,
        'symbol': response.symbol,
        'timeframe': response.timeframe,
        'candle_count': len(response.ohlcv),
        'latest_candle_at': latest_candle.isoformat(),
        'retrieved_at': response.retrieved_at.isoformat(),
        'latest_price': response.latest_price,
    }
    return snapshot, metadata


def _analysis(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    try:
        store, portfolio = _load_state()
    except Exception as exc:
        _log('runtime_unavailable', correlation_id=correlation_id, reason=type(exc).__name__)
        raise RuntimeError('PERSISTENCE_UNAVAILABLE') from exc

    try:
        _, market_metadata = _fresh_market_snapshot()
    except Exception:
        _log('analysis_no_trade', correlation_id=correlation_id, reason='MARKET_DATA_UNAVAILABLE')
        return {'result': 'NO_TRADE', 'reason': 'MARKET_DATA_UNAVAILABLE', 'correlation_id': correlation_id}

    broker = PaperBroker(portfolio)
    tools = ToolRegistry(
        portfolio=portfolio,
        broker=broker,
        store=store,
        allow_synthetic_market_data=False,
        allow_order_submission=False,
    )
    config = BedrockConfig(
        aws_region=os.environ.get('AWS_REGION'),
        bedrock_model_id=os.environ['BEDROCK_MODEL_ID'],
        max_agent_iterations=3,
        max_tool_calls=5,
    )
    provider = BedrockAgentModelProvider(config=config, tools=tools, analysis_only=True)
    prompt = event.get('prompt') or (
        'Analyze the supplied current BTC-USD paper-trading context. Return HOLD or NO_TRADE only. '
        'Do not submit an order or request credentials. '
        f'Market metadata: {json.dumps(market_metadata, sort_keys=True)}'
    )
    try:
        decision = provider.get_decision({'messages': [{'role': 'user', 'text': prompt}]})
    except Exception as exc:
        reason = str(exc)
        _log('analysis_no_trade', correlation_id=correlation_id, reason=reason)
        return {
            'result': 'NO_TRADE',
            'reason': reason,
            'correlation_id': correlation_id,
            'market': market_metadata,
        }

    action = decision.action if decision.action in ('HOLD', 'NO_TRADE') else 'NO_TRADE'
    reason = 'ANALYSIS_ONLY' if action == decision.action else 'ACTION_SUPPRESSED_ANALYSIS_ONLY'
    _log('analysis_complete', correlation_id=correlation_id, action=action, reason=reason)
    return {
        'result': action,
        'reason': reason,
        'correlation_id': correlation_id,
        'model_id': EXPECTED_MODEL_ID,
        'confidence': decision.confidence,
        'market': market_metadata,
    }


def _paper_order(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    store, portfolio = _load_state()
    market, market_metadata = _fresh_market_snapshot()
    proposal_data = event.get('proposal')
    if not isinstance(proposal_data, dict):
        raise ValueError('proposal must be an object')
    proposal = AgentTradeProposal(**proposal_data)
    if proposal.action not in ('BUY', 'SELL'):
        raise ValueError('paper_order action must be BUY or SELL')
    if not proposal.idempotency_key:
        raise ValueError('paper_order requires an idempotency_key')

    broker = PaperBroker(portfolio)
    service = AgentTradeService(broker=broker, store=store)
    portfolio_snapshot = portfolio.snapshot(market.price)
    portfolio_snapshot['portfolio_id'] = PORTFOLIO_ID
    request = PaperOrderRequest(
        proposal=proposal,
        portfolio_snapshot=portfolio_snapshot,
        market_snapshot=market,
    )
    try:
        result = service.submit_paper_order(request)
    except PaperOrderRejected as exc:
        _log('paper_order_rejected', correlation_id=correlation_id, reason_code=exc.details.reason_code)
        return {
            'result': 'REJECTED',
            'reason_code': exc.details.reason_code,
            'correlation_id': correlation_id,
            'market': market_metadata,
        }

    payload = result.model_dump(mode='json')
    _log(
        'paper_order_committed',
        correlation_id=correlation_id,
        order_id=result.paper_order_id,
        fill_id=result.fill_id,
        risk_decision_id=result.risk_decision_id,
    )
    return {
        'result': 'PAPER_ORDER_COMMITTED',
        'correlation_id': correlation_id,
        'paper_order': payload,
        'market': market_metadata,
    }


def _auto_dip(correlation_id: str) -> Dict[str, Any]:
    """Run automatic dip-buy strategy once and return evaluation summary."""
    store, portfolio = _load_state()
    broker = PaperBroker(portfolio)
    strategy = DipBuyStrategy(store=store, broker=broker, config=_dip_config())
    res = strategy.evaluate_and_maybe_execute()
    # attach correlation and return
    res['correlation_id'] = correlation_id
    return res


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Private paper-only runtime. It has no public endpoint or live broker path."""
    _check_env()
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    correlation_id = str(event.get('correlation_id') or uuid.uuid4())
    action = event.get('action', 'analyze')
    if action == 'analyze':
        return _analysis(event, correlation_id)
    if action == 'paper_order':
        try:
            return _paper_order(event, correlation_id)
        except ValidationError as exc:
            fields = {str(error.get('loc', ('',))[0]) for error in exc.errors()}
            if 'symbol' in fields:
                reason_code = 'INVALID_ASSET'
            elif 'leverage' in fields:
                reason_code = 'LEVERAGE_NOT_ALLOWED'
            else:
                reason_code = 'INVALID_INPUT'
            _log('paper_order_rejected', correlation_id=correlation_id, reason_code=reason_code)
            return {
                'result': 'REJECTED',
                'reason_code': reason_code,
                'correlation_id': correlation_id,
            }
    if action == 'health':
        _load_state()
        _log('health_ok', correlation_id=correlation_id)
        cfg = _dip_config()
        return {
            'result': 'OK',
            'mode': 'PAPER',
            'symbol': 'BTC-USD',
            'correlation_id': correlation_id,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'automatic_dip_buy': {
                'enabled': cfg.enabled,
                'symbol': cfg.symbol,
                'threshold_pct': cfg.dip_threshold_pct,
                'lookback_minutes': cfg.lookback_minutes,
                'paper_order_usd': cfg.paper_order_usd,
                'cooldown_minutes': cfg.cooldown_minutes,
                'schedule_minutes': 5,
                'decision_source': 'DETERMINISTIC_RULE',
            },
        }
    if action == 'auto_dip_evaluate':
        try:
            return _auto_dip(correlation_id)
        except Exception as exc:
            _log('auto_dip_failed', correlation_id=correlation_id, reason=type(exc).__name__)
            raise
    raise ValueError('unsupported action')
