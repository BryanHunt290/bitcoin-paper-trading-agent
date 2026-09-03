from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
import threading


class TradeEventStore:
    def get_latest_portfolio_snapshot(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError()

    def save_signal(self, signal_id: str, data: Dict[str, Any]):
        raise NotImplementedError()

    def save_risk_decision(self, risk_id: str, data: Dict[str, Any]):
        raise NotImplementedError()

    def save_order(self, order_id: str, data: Dict[str, Any]):
        raise NotImplementedError()

    def save_fill(self, fill_id: str, data: Dict[str, Any]):
        raise NotImplementedError()

    def save_portfolio_snapshot(self, snapshot_id: str, data: Dict[str, Any]):
        raise NotImplementedError()

    def get_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError()

    def get_strategy_state(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError()

    def save_strategy_state(self, strategy_name: str, state: Dict[str, Any]):
        raise NotImplementedError()

    def atomic_trade_commit(self, *, idempotency_key: str, idempotency_fingerprint: str, risk_id: str, risk_obj: Dict[str, Any], order_id: str, order_obj: Dict[str, Any], fill_id: str, fill_obj: Dict[str, Any], portfolio_id: str, new_portfolio_snapshot: Dict[str, Any], new_risk_state: Dict[str, Any], expected_risk_version: int, strategy_state_name: str | None = None, strategy_state: Dict[str, Any] | None = None):
        """Atomically commit idempotency, risk decision, order, fill, snapshot, and risk state.

        Implementations must ensure all updates become visible together or none at all.
        """
        raise NotImplementedError()


@dataclass
class InMemoryTradeEventStore(TradeEventStore):
    signals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    risks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    orders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    fills: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    latest_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    risk_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    strategy_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    idempotency_index: Dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def save_signal(self, signal_id: str, data: Dict[str, Any]):
        with self.lock:
            self.signals[signal_id] = data

    def save_risk_decision(self, risk_id: str, data: Dict[str, Any]):
        with self.lock:
            self.risks[risk_id] = data

    def save_order(self, order_id: str, data: Dict[str, Any]):
        with self.lock:
            idemp = data.get('idempotency_key')
            fingerprint = data.get('request_fingerprint')
            if idemp:
                entry = self.idempotency_index.get(idemp)
                if entry:
                    existing_order_id, existing_fingerprint = entry
                    if fingerprint == existing_fingerprint:
                        return self.orders.get(existing_order_id)
                    else:
                        # conflict: same key different request
                        return {'conflict': True, 'existing_order_id': existing_order_id, 'existing_fingerprint': existing_fingerprint}
                self.idempotency_index[idemp] = (order_id, fingerprint)
            self.orders[order_id] = data
            return data

    def save_fill(self, fill_id: str, data: Dict[str, Any]):
        with self.lock:
            self.fills[fill_id] = data

    def save_portfolio_snapshot(self, snapshot_id: str, data: Dict[str, Any]):
        with self.lock:
            self.snapshots[snapshot_id] = data

    def get_latest_portfolio_snapshot(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            snapshot = self.latest_snapshots.get(portfolio_id)
            return dict(snapshot) if snapshot else None

    def get_by_idempotency_key(self, key: str):
        with self.lock:
            entry = self.idempotency_index.get(key)
            if not entry:
                return None
            order_id, fingerprint = entry
            return {'order': self.orders.get(order_id), 'fingerprint': fingerprint}

    # Portfolio risk state persistence API (versioned optimistic concurrency)
    def get_portfolio_risk_state(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            entry = self.risk_states.get(portfolio_id)
            if not entry:
                return None
            # return a shallow copy to prevent accidental mutation
            state, version = entry['state'], entry['version']
            return {'state': dict(state), 'version': version}

    # Strategy state API for automatic strategies (keyed by strategy name)
    def get_strategy_state(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            state = self.strategy_states.get(strategy_name)
            return dict(state) if state is not None else None

    def save_strategy_state(self, strategy_name: str, state: Dict[str, Any]):
        with self.lock:
            self.strategy_states[strategy_name] = dict(state)

    def initialize_portfolio_risk_state(self, portfolio_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if portfolio_id in self.risk_states:
                return {'conflict': True, 'existing_version': self.risk_states[portfolio_id]['version']}
            self.risk_states[portfolio_id] = {'state': dict(state), 'version': 1}
            return {'state': dict(state), 'version': 1}

    def save_portfolio_risk_state(self, portfolio_id: str, state: Dict[str, Any], expected_version: int) -> Dict[str, Any]:
        with self.lock:
            entry = self.risk_states.get(portfolio_id)
            if not entry:
                return {'error': 'NOT_FOUND'}
            current_version = entry['version']
            if expected_version != current_version:
                return {'conflict': True, 'current_version': current_version}
            # increment version and persist
            self.risk_states[portfolio_id] = {'state': dict(state), 'version': current_version + 1}
            return {'state': dict(state), 'version': current_version + 1}

    def atomic_trade_commit(self, *, idempotency_key: str, idempotency_fingerprint: str, risk_id: str, risk_obj: Dict[str, Any], order_id: str, order_obj: Dict[str, Any], fill_id: str, fill_obj: Dict[str, Any], portfolio_id: str, new_portfolio_snapshot: Dict[str, Any], new_risk_state: Dict[str, Any], expected_risk_version: int, strategy_state_name: str | None = None, strategy_state: Dict[str, Any] | None = None):
        # Provide transactional semantics in-memory: lock and validate then commit all changes.
        with self.lock:
            # idempotency uniqueness
            existing = self.idempotency_index.get(idempotency_key)
            if existing:
                existing_order_id, existing_fingerprint = existing
                if existing_fingerprint == idempotency_fingerprint:
                    # return existing committed result
                    return {'ExistingIdempotency': {'order_id': existing_order_id}}
                # conflict
                return {'conflict': 'IDEMPOTENCY_CONFLICT'}

            # risk-state version check
            rs = self.risk_states.get(portfolio_id)
            if not rs:
                return {'error': 'RISK_STATE_NOT_FOUND'}
            if rs['version'] != expected_risk_version:
                return {'conflict': 'RISK_STATE_CONFLICT', 'current_version': rs['version']}

            # apply all writes
            # idempotency index maps key -> (order_id, fingerprint)
            self.idempotency_index[idempotency_key] = (order_id, idempotency_fingerprint)
            # save risk decision
            self.risks[risk_id] = risk_obj
            # save order
            self.orders[order_id] = {'idempotency_key': idempotency_key, 'request_fingerprint': idempotency_fingerprint, 'result': order_obj, 'timestamp': new_portfolio_snapshot.get('updated_at')}
            # save fill
            self.fills[fill_id] = fill_obj
            # save snapshot (use timestamp key)
            from datetime import timezone
            snap_key = f"SNAPSHOT#{datetime.now(timezone.utc).isoformat()}"
            self.snapshots[snap_key] = new_portfolio_snapshot
            self.latest_snapshots[portfolio_id] = dict(new_portfolio_snapshot)
            # advance risk state version
            self.risk_states[portfolio_id] = {'state': dict(new_risk_state), 'version': expected_risk_version + 1}
            if strategy_state_name and strategy_state is not None:
                committed_strategy_state = dict(strategy_state)
                committed_strategy_state['last_trade_id'] = order_id
                self.strategy_states[strategy_state_name] = committed_strategy_state

            return {'order_id': order_id, 'fill_id': fill_id}
