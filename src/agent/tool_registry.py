from __future__ import annotations
from typing import Any, Dict, Callable, Optional
from datetime import datetime, timezone, timedelta
from .models import MarketDataSnapshot, AgentDecision, AgentTradeProposal
from ..portfolio.portfolio import Portfolio
from ..risk.risk_engine import calculate_position_size, RiskConfig
from ..broker.paper_broker import PaperBroker
from ..market_data.adapter import get_market_data as adapter_get_market_data
from .tools import AgentTradeService
import logging

logger = logging.getLogger(__name__)


class UnknownToolError(Exception):
    pass


class ToolRegistry:
    def __init__(self, portfolio: Portfolio, broker: PaperBroker, store=None, risk_config: Optional[RiskConfig]=None, allow_synthetic_market_data: bool = False, allow_order_submission: bool = True):
        self.portfolio = portfolio
        self.broker = broker
        self.risk_config = risk_config or RiskConfig()
        self._service = AgentTradeService(broker, store=store, risk_config=self.risk_config)
        self.allow_synthetic_market_data = allow_synthetic_market_data
        # allowlist mapping
        self.tools: Dict[str, Callable] = {
            'get_market_data': self.get_market_data,
            'calculate_indicators': self.calculate_indicators,
            'get_portfolio': self.get_portfolio,
            'get_strategy_performance': self.get_strategy_performance,
            'run_backtest': self.run_backtest,
            'analyze_previous_trades': self.analyze_previous_trades,
            'explain_trade': self.explain_trade,
        }
        if allow_order_submission:
            self.tools['submit_paper_order'] = self.submit_paper_order

    def call(self, name: str, *args, **kwargs):
        if name not in self.tools:
            logger.warning('Unknown tool requested: %s', name)
            raise UnknownToolError(f'Unknown tool: {name}')
        return self.tools[name](*args, **kwargs)

    def get_market_data(self, symbol: str, timeframe: str, candle_count: int) -> MarketDataSnapshot:
        if symbol != 'BTC-USD':
            raise ValueError('Only BTC-USD supported')
        # try adapter but fall back to simple snapshot if network unavailable
        try:
            resp = adapter_get_market_data(symbol, timeframe, candle_count)
            price = resp.latest_price
            ts = resp.retrieved_at
            # validate retrieved values
            if price is None or ts is None:
                raise ValueError('Market data adapter returned incomplete data')
            return MarketDataSnapshot(symbol='BTC-USD', price=price, timestamp=ts)
        except Exception as e:
            if self.allow_synthetic_market_data:
                now = datetime.now(timezone.utc)
                return MarketDataSnapshot(symbol='BTC-USD', price=30000.0, timestamp=now)
            # production: fail closed
            raise RuntimeError(f'Market data unavailable: {e}')

    def calculate_indicators(self, candles: list) -> Dict[str, float]:
        # Minimal deterministic indicators for offline testing
        closes = [c['close'] for c in candles]
        if not closes:
            raise ValueError('No candles')
        def ema(values, period):
            k = 2/(period+1)
            ema_v = values[0]
            for v in values[1:]:
                ema_v = v*k + ema_v*(1-k)
            return ema_v
        def rsi(values, period=14):
            gains = 0.0
            losses = 0.0
            for i in range(1, min(len(values), period+1)):
                diff = values[i]-values[i-1]
                if diff>0: gains+=diff
                else: losses-=diff
            if losses==0:
                return 100.0
            rs = gains/losses
            return 100 - (100/(1+rs))

        return {
            'EMA20': ema(closes[-20:] if len(closes)>=20 else closes, 20),
            'EMA50': ema(closes[-50:] if len(closes)>=50 else closes, 50),
            'RSI14': rsi(closes, 14),
            'ATR14': 0.0,
        }

    def get_portfolio(self, market_price: float) -> Dict[str, Any]:
        return self.portfolio.snapshot(market_price)

    def get_strategy_performance(self, strategy_id: str) -> Dict[str, Any]:
        # Minimal deterministic stub for approved strategy
        if strategy_id not in {'ema_cross_v1'}:
            raise ValueError('Unsupported strategy')
        return {'total_return': 0.0, 'net_pnl': 0.0, 'trades': 0, 'win_rate': 0.0, 'avg_winner': 0.0, 'avg_loser': 0.0, 'profit_factor': 0.0, 'max_drawdown': 0.0, 'fees': 0.0, 'slippage': 0.0, 'avg_holding_period': 0.0}

    def run_backtest(self, strategy_id: str, historical_candles: list, fees: float = 0.0, slippage: float = 0.0) -> Dict[str, Any]:
        # Use simple deterministic backtest stub
        if strategy_id not in {'ema_cross_v1'}:
            raise ValueError('Unsupported strategy')
        return {'total_return': 0.0, 'trades': 0, 'diagnostics': 'stubbed'}

    def analyze_previous_trades(self, trades: list) -> Dict[str, Any]:
        return {'trending': False, 'volatility': 'LOW', 'summary': 'stubbed'}

    def submit_paper_order(self, decision: AgentDecision, market_snapshot: MarketDataSnapshot) -> Dict[str, Any]:
        # Convert AgentDecision to AgentTradeProposal and call AgentTradeService
        qty = None
        if decision.requested_notional_usd and getattr(market_snapshot, 'price', None):
            try:
                qty = float(decision.requested_notional_usd) / float(market_snapshot.price)
            except Exception:
                qty = None

        proposal = AgentTradeProposal(
            symbol=decision.symbol,
            action=decision.action,
            strategy_id=decision.strategy_id,
            requested_notional_usd=decision.requested_notional_usd,
            requested_quantity=qty,
            stop_price=decision.stop_price,
            timestamp=decision.timestamp,
            idempotency_key=decision.idempotency_key,
            execution_mode='PAPER',
            allow_risk_resizing=True,
        )
        req = type('R', (), {})()
        req.proposal = proposal
        req.portfolio_snapshot = self.get_portfolio(market_snapshot.price)
        req.market_snapshot = market_snapshot
        result = self._service.submit_paper_order(req)
        return result.model_dump() if hasattr(result, 'model_dump') else dict(result)

    def explain_trade(self, decision: AgentDecision, market_snapshot: MarketDataSnapshot) -> Dict[str, Any]:
        return {
            'action': decision.action,
            'strategy': decision.strategy_id,
            'market_regime': decision.market_regime,
            'supporting_evidence': decision.evidence or [],
            'requested_notional': decision.requested_notional_usd,
            'approved_size': None,
            'confidence': decision.confidence,
        }
