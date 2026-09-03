from typing import Callable, Optional
from datetime import datetime, timedelta, timezone
import uuid
from pydantic import ValidationError

from .models import AgentTradeProposal, MarketDataSnapshot, PaperOrderRequest, RiskRequest, RiskDecision, PaperOrderResult, RejectionReason
from .store import InMemoryTradeEventStore, TradeEventStore
from .exceptions import PaperOrderRejected
import json, hashlib
from ..risk.risk_engine import calculate_position_size, RiskConfig
from ..broker.paper_broker import PaperBroker
from ..reporter.s3_reporter import build_paper_performance_report, write_report_best_effort


SUPPORTED_STRATEGIES = {'ema_cross_v1', 'dip_buy_v1'}


class AgentTradeService:
    def __init__(
        self,
        broker: PaperBroker,
        store: Optional[TradeEventStore] = None,
        risk_config: RiskConfig = RiskConfig(),
        report_writer: Callable[[dict], object] | None = None,
    ):
        self.broker = broker
        self.store = store or InMemoryTradeEventStore()
        self.risk_config = risk_config
        self.report_writer = report_writer or write_report_best_effort

    def _validate_proposal(self, proposal: AgentTradeProposal, market: MarketDataSnapshot):
        # symbol check
        if proposal.symbol.value != 'BTC-USD':
            return False, RejectionReason.INVALID_ASSET
        # execution mode
        if proposal.execution_mode != proposal.execution_mode.PAPER:
            return False, RejectionReason.PAPER_MODE_REQUIRED
        # strategy id
        if proposal.strategy_id not in SUPPORTED_STRATEGIES:
            return False, RejectionReason.UNSUPPORTED_STRATEGY
        # timestamps
        if not isinstance(proposal.timestamp, datetime) or not isinstance(market.timestamp, datetime):
            return False, RejectionReason.OTHER
        # stale market data (older than 5 mins)
        if datetime.now(timezone.utc) - market.timestamp > timedelta(minutes=5):
            return False, RejectionReason.STALE_MARKET_DATA
        # check notional/quantity validity
        if proposal.requested_notional_usd is None and proposal.requested_quantity is None:
            return False, RejectionReason.INVALID_NOTIONAL
        if proposal.requested_notional_usd is not None and proposal.requested_notional_usd < 0:
            return False, RejectionReason.INVALID_NOTIONAL
        if proposal.requested_quantity is not None and proposal.requested_quantity <= 0:
            return False, RejectionReason.INVALID_QUANTITY
        # no leverage or shorting allowed — agents could include these fields but model doesn't; assume safe
        return True, None

    def submit_paper_order(
        self,
        request: PaperOrderRequest,
        *,
        strategy_state_name: str | None = None,
        strategy_state: dict | None = None,
        strategy_state_builder: Callable[[dict], dict] | None = None,
    ) -> PaperOrderResult:
        # validate Pydantic models
        try:
            proposal = request.proposal
            market = request.market_snapshot
        except ValidationError as e:
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.OTHER.value, message='Malformed proposal'))

        ok, reason = self._validate_proposal(proposal, market)
        risk_id = f"RISK#{uuid.uuid4().hex}"
        # prepare risk request
        risk_req = {
            'portfolio_snapshot': request.portfolio_snapshot,
            'market_snapshot': market.model_dump(),
            'requested_notional_usd': proposal.requested_notional_usd or 0.0,
            'requested_quantity': proposal.requested_quantity,
            'action': proposal.action,
            'strategy_id': proposal.strategy_id,
        }
        # do not persist preliminary risk decision here; final decision will be persisted atomically on commit

        if not ok:
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=str(reason.value if hasattr(reason, 'value') else reason), message='Pre-validation failed', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # Resolve committed retries before evaluating mutable portfolio/risk state.
        # The immutable proposal defines request identity; market and portfolio
        # state naturally change after a successful commit.
        canonical = json.dumps({'proposal': proposal.model_dump()}, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        idempotency_key = proposal.idempotency_key or f"auto#{uuid.uuid4().hex}"
        existing = self.store.get_by_idempotency_key(idempotency_key)
        if existing:
            existing_order = existing.get('order')
            existing_fingerprint = existing.get('fingerprint')
            if existing_order and existing_fingerprint == fingerprint:
                return PaperOrderResult(**existing_order['result'])
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code='IDEMPOTENCY_CONFLICT', message='Same idempotency key with different request', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # SELL-specific validation: must have quantity and holdings
        if proposal.action == 'SELL':
            # require a requested_quantity for SELL
            if not proposal.requested_quantity or proposal.requested_quantity <= 0:
                from .exceptions import PaperOrderRejectionModel
                raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.INVALID_QUANTITY.value, message='SELL rejected: invalid quantity', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))
            if request.portfolio_snapshot.get('btc_quantity', 0.0) <= 0:
                from .exceptions import PaperOrderRejectionModel
                raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.INVALID_QUANTITY.value, message='SELL rejected: no BTC holdings', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))
            if proposal.requested_quantity > request.portfolio_snapshot.get('btc_quantity', 0.0):
                from .exceptions import PaperOrderRejectionModel
                raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.INVALID_QUANTITY.value, message='SELL rejected: quantity exceeds holdings', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # compute deterministic position size server-side
        portfolio_state = request.portfolio_snapshot.copy()
        portfolio_state['latest_price'] = market.price

        # load persisted portfolio risk state (versioned)
        portfolio_id = portfolio_state.get('portfolio_id', 'default')
        persisted = self.store.get_portfolio_risk_state(portfolio_id)
        persisted_state = None
        persisted_version = None
        if persisted:
            persisted_state = persisted.get('state')
            persisted_version = persisted.get('version')
        else:
            # Do NOT silently initialize for an established portfolio.
            # Determine if this is a genuinely new portfolio (no historical P&L, no BTC holdings)
            def _is_new_portfolio(snapshot: dict) -> bool:
                try:
                    starting = snapshot.get('starting_cash')
                    total = snapshot.get('total_equity')
                    btc_qty = snapshot.get('btc_quantity', 0.0)
                    realized = snapshot.get('realized_pnl', 0.0)
                    if starting is None or total is None:
                        return False
                    return float(starting) == float(total) and float(btc_qty) == 0.0 and float(realized) == 0.0
                except Exception:
                    return False

            if _is_new_portfolio(portfolio_state):
                # safe to initialize baseline for brand-new portfolio
                from .models import PortfolioRiskState
                now = datetime.now(timezone.utc)
                init = PortfolioRiskState(
                    portfolio_id=portfolio_id,
                    current_equity=portfolio_state.get('total_equity', self.risk_config.starting_capital),
                    peak_equity=portfolio_state.get('total_equity', self.risk_config.starting_capital),
                    day_start_equity=portfolio_state.get('total_equity', self.risk_config.starting_capital),
                    trading_day_utc=now.date().isoformat(),
                    daily_pnl=0.0,
                    current_drawdown=0.0,
                    updated_at=now,
                    version=1,
                )
                res = self.store.initialize_portfolio_risk_state(portfolio_id, init.model_dump())
                persisted_state = res.get('state')
                persisted_version = res.get('version')
            else:
                # Existing portfolio with missing persisted risk state: fail-closed for BUY exposure
                persisted_state = None
                persisted_version = None
                # If this is a BUY attempt, reject immediately due to missing authoritative risk state
                if proposal.action == 'BUY':
                    from .exceptions import PaperOrderRejectionModel
                    raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.OTHER.value, message='RISK_STATE_UNAVAILABLE for existing portfolio', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # For SELL requests, authorize reduction up to current holdings deterministically
        if proposal.action == 'SELL':
            approved_qty = min(proposal.requested_quantity or 0.0, portfolio_state.get('btc_quantity', 0.0))
            risk_out = {'allowed': True, 'reason_code': None, 'max_notional': approved_qty * market.price, 'position_size_btc': approved_qty, 'equity': portfolio_state.get('total_equity', 0.0)}
        else:
            # call deterministic engine for BUY using persisted state for drawdown/day baseline checks
            risk_out = calculate_position_size(proposal.requested_notional_usd or 0.0, portfolio_state, self.risk_config, persisted_state)
        # attach risk id
        risk_out['risk_decision_id'] = risk_id

        if not risk_out.get('allowed'):
            # persist updated decision for auditing
            self.store.save_risk_decision(risk_id, {'risk': risk_out, 'proposal': proposal.model_dump(), 'timestamp': datetime.now(timezone.utc).isoformat()})
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=str(risk_out.get('reason_code')), message='Risk engine rejected request', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id, risk_decision_id=risk_id, requested_notional=proposal.requested_notional_usd))

        # if sizes differ and agent did not allow resizing, reject
        approved_qty = risk_out.get('position_size_btc')
        if proposal.requested_quantity and abs(proposal.requested_quantity - approved_qty) > 1e-12 and not proposal.allow_risk_resizing:
            self.store.save_risk_decision(risk_id, {'risk': risk_out, 'proposal': proposal.model_dump(), 'timestamp': datetime.now(timezone.utc).isoformat()})
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.INVALID_QUANTITY.value, message='Requested quantity differs from approved and resizing not allowed', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id, risk_decision_id=risk_id, requested_notional=proposal.requested_notional_usd, approved_notional=risk_out.get('max_notional')))

        # execute via broker internal API (compute fill but DO NOT apply to portfolio yet)
        # SELL rules: only reduce existing long positions
        if proposal.action == 'SELL':
            if portfolio_state.get('btc_quantity', 0.0) <= 0:
                from .exceptions import PaperOrderRejectionModel
                raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.INVALID_QUANTITY.value, message='SELL rejected: no BTC holdings', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))
            if approved_qty > portfolio_state.get('btc_quantity', 0.0):
                from .exceptions import PaperOrderRejectionModel
                raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=RejectionReason.INVALID_QUANTITY.value, message='SELL rejected: quantity exceeds holdings', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # call internal broker exec
        broker_event = self.broker._execute_order(proposal.action, approved_qty, market.price, proposal.strategy_id, proposal.timestamp, apply_to_portfolio=False)

        # prepare order and fill identifiers and candidate result (not yet committed)
        order_id = f"ORDER#{uuid.uuid4().hex}"
        fill_id = f"FILL#{uuid.uuid4().hex}"
        candidate_result = {
            'paper_order_id': order_id,
            'fill_id': fill_id,
            'filled_quantity': broker_event['quantity'],
            'filled_price': broker_event['executed_price'],
            'fees': broker_event['fees'],
            'slippage': broker_event['slippage'],
            'portfolio_state': broker_event['portfolio'],  # candidate snapshot
            'risk_decision_id': risk_id,
        }

        final_strategy_state = strategy_state
        if strategy_state_builder is not None:
            try:
                final_strategy_state = strategy_state_builder(dict(candidate_result))
            except Exception as e:
                from .exceptions import PaperOrderRejectionModel
                raise PaperOrderRejected(PaperOrderRejectionModel(reason_code='STRATEGY_STATE_INVALID', message=str(e), correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))
            if not isinstance(final_strategy_state, dict):
                from .exceptions import PaperOrderRejectionModel
                raise PaperOrderRejected(PaperOrderRejectionModel(reason_code='STRATEGY_STATE_INVALID', message='Strategy state builder must return an object', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # perform authoritative, atomic commit via store adapter
        # compute the resulting risk state deterministically from persisted_state and candidate portfolio
        new_equity = broker_event['portfolio'].get('total_equity')
        prev_state = persisted_state or {}
        new_risk_state = dict(prev_state) if isinstance(prev_state, dict) else {}
        prev_peak = new_risk_state.get('peak_equity', new_equity)
        if new_equity is not None:
            if new_equity > prev_peak:
                new_risk_state['peak_equity'] = new_equity
                new_risk_state['current_drawdown'] = 0.0
            else:
                if prev_peak and prev_peak > 0:
                    new_risk_state['current_drawdown'] = (prev_peak - new_equity) / prev_peak
        # day rollover handling
        today = datetime.now(timezone.utc).date().isoformat()
        if new_risk_state.get('trading_day_utc') != today:
            new_risk_state['day_start_equity'] = new_risk_state.get('current_equity', new_equity)
            new_risk_state['trading_day_utc'] = today
        new_risk_state['current_equity'] = new_equity
        new_risk_state['daily_pnl'] = new_equity - new_risk_state.get('day_start_equity', new_equity)
        new_risk_state['updated_at'] = datetime.now(timezone.utc).isoformat()

        try:
            commit_args = dict(
                idempotency_key=idempotency_key,
                idempotency_fingerprint=fingerprint,
                risk_id=risk_id,
                risk_obj={'risk': risk_out, 'proposal': proposal.model_dump(), 'timestamp': datetime.now(timezone.utc).isoformat()},
                order_id=order_id,
                order_obj=candidate_result,
                fill_id=fill_id,
                fill_obj=broker_event,
                portfolio_id=portfolio_id,
                new_portfolio_snapshot=broker_event['portfolio'],
                new_risk_state=new_risk_state,
                expected_risk_version=persisted_version or 0,
            )
            if strategy_state_name and final_strategy_state is not None:
                commit_args.update(
                    strategy_state_name=strategy_state_name,
                    strategy_state=final_strategy_state,
                )
            commit_res = self.store.atomic_trade_commit(**commit_args)
        except Exception as e:
            # map store errors to rejection
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=str(e), message='Commit failed', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # if existing idempotency was found during commit, return that result
        if isinstance(commit_res, dict) and commit_res.get('ExistingIdempotency'):
            existing = self.store.get_by_idempotency_key(idempotency_key)
            if existing and existing.get('order'):
                return PaperOrderResult(**existing['order']['result'])
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code='IDEMPOTENCY_CONFLICT', message='Idempotency conflict after commit', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        if isinstance(commit_res, dict) and commit_res.get('conflict'):
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=str(commit_res.get('conflict')), message='Commit conflict', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        if isinstance(commit_res, dict) and commit_res.get('error'):
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code=str(commit_res.get('error')), message='Commit failed closed', correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # The authoritative paper transaction is complete. Reporting is optional
        # and best-effort, so an S3 outage can never reverse or reject the trade.
        try:
            report = build_paper_performance_report(
                proposal=proposal.model_dump(),
                order_result=candidate_result,
                portfolio_state=broker_event['portfolio'],
                risk_state=new_risk_state,
                committed_at=broker_event['timestamp'],
            )
            self.report_writer(report)
        except Exception:
            pass

        # Commit succeeded. Apply the exact committed fill to the local object;
        # do not recalculate slippage, fees, identifiers, or candidate state.
        try:
            self.broker.portfolio.apply_fill(
                proposal.action,
                broker_event['quantity'],
                broker_event['executed_price'],
                broker_event['fees'],
            )
            applied_portfolio = self.broker.portfolio.snapshot(broker_event['executed_price'])
        except Exception as e:
            # broker apply failed after authoritative commit — surface error but do not roll back DB
            from .exceptions import PaperOrderRejectionModel
            raise PaperOrderRejected(PaperOrderRejectionModel(reason_code='BROKER_APPLY_FAILED', message=str(e), correlation_id=str(uuid.uuid4()), signal_id=proposal.idempotency_key, strategy_id=proposal.strategy_id))

        # finalize result using applied portfolio snapshot
        candidate_result['portfolio_state'] = applied_portfolio
        return PaperOrderResult(**candidate_result)
