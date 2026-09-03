from __future__ import annotations
from typing import Optional, Dict, Any, List
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
import time
import random
from typing import Tuple


class DynamoError(RuntimeError):
    pass


class DynamoTradeEventStore:
    """DynamoDB persistence adapter implementing transactional, idempotent operations.

    Single-table design with PK/SK and entity prefixes. Supports injected fake resources
    for unit testing.
    """

    def __init__(self, table_name: str = 'trade-events', dynamodb_resource: Optional[Any] = None, region_name: Optional[str] = None, dynamodb_client: Optional[Any] = None):
        self.table_name = table_name
        self._dynamodb = dynamodb_resource
        self._dynamodb_client = dynamodb_client
        self._resource_was_injected = dynamodb_resource is not None
        self.region_name = region_name

    def _ensure_resource(self):
        if self._dynamodb is None:
            try:
                import boto3
                self._dynamodb = boto3.resource('dynamodb', region_name=self.region_name)
            except Exception as e:
                raise DynamoError('boto3 required for DynamoTradeEventStore') from e

    def _table(self):
        self._ensure_resource()
        return self._dynamodb.Table(self.table_name)

    def _client(self):
        if self._dynamodb_client is not None:
            return self._dynamodb_client
        if self._resource_was_injected:
            # Test doubles historically expose their transaction client here.
            self._ensure_resource()
            return getattr(self._dynamodb, 'meta').client
        # Atomic transactions below are constructed with low-level AttributeValue
        # maps, so use a raw client. A resource-owned client installs a serializer
        # that would encode those maps a second time (S -> M).
        try:
            import boto3
            self._dynamodb_client = boto3.client('dynamodb', region_name=self.region_name)
        except Exception as e:
            raise DynamoError('boto3 required for DynamoTradeEventStore') from e
        return self._dynamodb_client

    @staticmethod
    def _to_decimal(obj: Any) -> Any:
        # Convert floats to Decimal recursively for DynamoDB compatibility
        if isinstance(obj, float):
            return Decimal(str(obj))
        if isinstance(obj, dict):
            return {k: DynamoTradeEventStore._to_decimal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [DynamoTradeEventStore._to_decimal(v) for v in obj]
        return obj

    @staticmethod
    def _require_tz(dt: datetime) -> str:
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise ValueError('naive datetime not allowed; use timezone-aware UTC')
        return dt.astimezone(timezone.utc).isoformat()

    # Table schema (single table)
    # PK: 'PK' (string)   e.g. 'PORTFOLIO#<id>' or 'ORDER#<id>' or 'IDEMPOTENCY#<key>'
    # SK: 'SK' (string)   e.g. 'RISK_STATE', 'META', 'SNAPSHOT#<ts>'

    def get_portfolio_risk_state(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        tbl = self._table()
        resp = tbl.get_item(Key={'PK': f'PORTFOLIO#{portfolio_id}', 'SK': 'RISK_STATE'})
        item = resp.get('Item')
        if not item:
            return None
        # state_json may contain numbers serialized as strings (Decimal->str).
        # Normalize known numeric fields before risk calculations consume them.
        raw = item.get('state_json')
        try:
            state = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR') from exc
        if not isinstance(state, dict):
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR')

        numeric_fields = (
            'current_equity',
            'peak_equity',
            'day_start_equity',
            'daily_pnl',
            'current_drawdown',
        )
        try:
            for key in numeric_fields:
                if key in state:
                    state[key] = float(state[key])
                    if not math.isfinite(state[key]):
                        raise ValueError(f'{key} must be finite')
        except (TypeError, ValueError, OverflowError) as exc:
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR') from exc

        return {'state': state, 'version': int(item.get('version', 0))}

    def get_latest_portfolio_snapshot(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        tbl = self._table()
        resp = tbl.query(
            KeyConditionExpression='PK = :pk AND begins_with(SK, :prefix)',
            ExpressionAttributeValues={
                ':pk': f'PORTFOLIO#{portfolio_id}',
                ':prefix': 'SNAPSHOT#',
            },
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get('Items') or []
        if not items:
            return None
        snapshot = items[0].get('snapshot')
        if not isinstance(snapshot, str):
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR')
        try:
            return json.loads(snapshot)
        except (TypeError, ValueError) as exc:
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR') from exc

    def initialize_portfolio_risk_state(self, portfolio_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        tbl = self._table()
        now = datetime.now(timezone.utc).isoformat()
        item = {
            'PK': f'PORTFOLIO#{portfolio_id}',
            'SK': 'RISK_STATE',
            'state_json': json.dumps(state, default=str),
            'version': 1,
            'updated_at': now,
        }
        try:
            tbl.put_item(Item=item, ConditionExpression='attribute_not_exists(PK)')
        except Exception as e:
            raise DynamoError('DYNAMODB_CONDITIONAL_CONFLICT') from e
        return {'state': state, 'version': 1}

    def save_portfolio_risk_state(self, portfolio_id: str, state: Dict[str, Any], expected_version: int) -> Dict[str, Any]:
        tbl = self._table()
        now = datetime.now(timezone.utc).isoformat()
        new_version = expected_version + 1
        item = {
            'PK': f'PORTFOLIO#{portfolio_id}',
            'SK': 'RISK_STATE',
            'state_json': json.dumps(state, default=str),
            'version': new_version,
            'updated_at': now,
        }
        try:
            tbl.put_item(Item=item, ConditionExpression='version = :v', ExpressionAttributeValues={':v': expected_version})
        except Exception as e:
            raise DynamoError('RISK_STATE_CONFLICT') from e
        return {'state': state, 'version': new_version}

    def save_signal(self, signal_id: str, portfolio_id: str, payload: Dict[str, Any], timestamp: datetime):
        if not isinstance(timestamp, datetime):
            raise ValueError('timestamp must be datetime')
        t = self._require_tz(timestamp)
        tbl = self._table()
        item = {'PK': f'PORTFOLIO#{portfolio_id}', 'SK': f'SIGNAL#{t}#{signal_id}', 'payload': json.dumps(self._to_decimal(payload), default=str)}
        tbl.put_item(Item=item)

    def save_risk_decision(self, risk_id: str, payload: Dict[str, Any]):
        tbl = self._table()
        item = {'PK': f'RISK#{risk_id}', 'SK': 'META', 'payload': json.dumps(self._to_decimal(payload), default=str)}
        tbl.put_item(Item=item)

    def save_order(self, order_id: str, payload: Dict[str, Any]):
        tbl = self._table()
        item = {'PK': f'ORDER#{order_id}', 'SK': 'META', 'payload': json.dumps(self._to_decimal(payload), default=str)}
        tbl.put_item(Item=item)

    def save_fill(self, fill_id: str, payload: Dict[str, Any]):
        tbl = self._table()
        item = {'PK': f'FILL#{fill_id}', 'SK': 'META', 'payload': json.dumps(self._to_decimal(payload), default=str)}
        tbl.put_item(Item=item)

    def save_portfolio_snapshot(self, portfolio_id: str, snapshot: Dict[str, Any], timestamp: datetime):
        t = self._require_tz(timestamp)
        tbl = self._table()
        item = {'PK': f'PORTFOLIO#{portfolio_id}', 'SK': f'SNAPSHOT#{t}', 'snapshot': json.dumps(self._to_decimal(snapshot), default=str)}
        tbl.put_item(Item=item)

    # Strategy state persistence for automatic strategies
    def get_strategy_state(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        tbl = self._table()
        resp = tbl.get_item(Key={'PK': f'STRATEGY#{strategy_name}', 'SK': 'STATE'})
        item = resp.get('Item')
        if not item:
            return None
        try:
            state = json.loads(item.get('state_json'))
        except (TypeError, ValueError) as exc:
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR') from exc
        if not isinstance(state, dict):
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR')
        return state

    def save_strategy_state(self, strategy_name: str, state: Dict[str, Any]):
        tbl = self._table()
        now = datetime.now(timezone.utc).isoformat()
        item = {'PK': f'STRATEGY#{strategy_name}', 'SK': 'STATE', 'state_json': json.dumps(self._to_decimal(state), default=str), 'updated_at': now}
        tbl.put_item(Item=item)

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        tbl = self._table()
        resp = tbl.get_item(Key={'PK': f'IDEMPOTENCY#{idempotency_key}', 'SK': 'META'})
        item = resp.get('Item')
        if not item:
            return None
        result_json = item.get('result')
        try:
            result = json.loads(result_json) if isinstance(result_json, str) else None
        except (TypeError, ValueError) as exc:
            raise DynamoError('DYNAMODB_SERIALIZATION_ERROR') from exc
        return {
            'order': {'result': result} if result else None,
            'fingerprint': item.get('fingerprint'),
            'order_id': item.get('order_id'),
        }

    def _transact_write(self, transact_items: List[Dict[str, Any]]):
        # Bounded retry wrapper for transient failures. Uses structured botocore exception
        # data when available and falls back to conservative textual classification.
        max_attempts = 4
        base_backoff = 0.12

        def _extract_idempotency_key(items: List[Dict[str, Any]]) -> Optional[str]:
            for it in items:
                if 'Put' in it:
                    item = it['Put'].get('Item') or {}
                    pk = None
                    if isinstance(item, dict):
                        if 'PK' in item:
                            v = item['PK']
                            if isinstance(v, dict) and 'S' in v:
                                pk = v['S']
                            else:
                                pk = v
                        elif item.get('PK'):
                            pk = item.get('PK')
                    if pk and isinstance(pk, str) and pk.startswith('IDEMPOTENCY#'):
                        return pk.split('#', 1)[1]
            return None

        # Attempt to import botocore exception types for structured handling
        try:
            from botocore.exceptions import ClientError, EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError
        except Exception:
            ClientError = None
            EndpointConnectionError = None
            ConnectTimeoutError = None
            ReadTimeoutError = None

        def _classify_exception(exc: Exception) -> Tuple[Optional[str], bool]:
            """Return (code, is_transient). code is one of the DynamoError labels or None if unknown.
            is_transient indicates whether it's safe to retry.
            """
            # Structured ClientError from botocore
            if ClientError is not None and isinstance(exc, ClientError):
                code = exc.response.get('Error', {}).get('Code', '')
                # Conditional / concurrency
                if code in ('ConditionalCheckFailedException', 'ConditionalCheckFailed'):
                    return 'DYNAMODB_CONDITIONAL_CONFLICT', False
                # Transaction errors
                if code in ('TransactionCanceledException',):
                    # attempt to parse CancellationReasons for a more-specific mapping
                    reasons = exc.response.get('CancellationReasons')
                    # If reasons missing or malformed, be conservative
                    if not isinstance(reasons, list) or len(reasons) == 0:
                        return 'DYNAMODB_TRANSACTION_CANCELED', False
                    # We expect the transact items to be constructed in a deterministic order
                    # See atomic_trade_commit: [idempotency, risk_decision, order, fill, snapshot, risk_state]
                    semantic_map = {
                        0: 'IDEMPOTENCY_CONFLICT',
                        1: None,  # risk decision (no special mapping)
                        2: None,  # order
                        3: None,  # fill
                        4: None,  # snapshot
                        5: 'RISK_STATE_CONFLICT',
                    }
                    # Only attempt semantic mapping when CancellationReasons length matches expected
                    if len(reasons) != len(semantic_map):
                        return 'DYNAMODB_TRANSACTION_CANCELED', False
                    conditional_fail_indices = []
                    meaningful_failures = 0
                    for idx, r in enumerate(reasons):
                        if not isinstance(r, dict):
                            continue
                        rc = (r.get('Code') or '')
                        # AWS uses 'None' for a non-failure entry in CancellationReasons
                        if rc in (None, '', 'None'):
                            continue
                        meaningful_failures += 1
                        # treat ConditionalCheckFailed as conditional failure
                        if 'ConditionalCheckFailed' in rc or rc in ('ConditionalCheckFailedException',):
                            conditional_fail_indices.append(idx)
                        else:
                            # other failure types present -> cannot map to conditional specifics
                            pass
                    # Only when exactly one meaningful failure exists and it's a conditional failure
                    if meaningful_failures == 1 and len(conditional_fail_indices) == 1:
                        failing_idx = conditional_fail_indices[0]
                        mapped = semantic_map.get(failing_idx)
                        if mapped:
                            return mapped, False
                        # other known item indices with conditional failure -> generic conditional conflict
                        return 'DYNAMODB_CONDITIONAL_CONFLICT', False
                    # ambiguous or multi-failure -> conservative
                    return 'DYNAMODB_TRANSACTION_CANCELED', False
                if code in ('TransactionConflictException',):
                    return 'DYNAMODB_CONDITIONAL_CONFLICT', False
                # Throttling / capacity
                if code in ('ProvisionedThroughputExceededException', 'ThrottlingException', 'RequestLimitExceeded'):
                    return 'DYNAMODB_THROTTLED', True
                # Service availability
                if code in ('InternalServerError', 'InternalServerErrorException', 'ServiceUnavailable') or str(code).startswith('5'):
                    return 'DYNAMODB_UNAVAILABLE', True
                # Authorization
                if code in ('AccessDeniedException', 'AccessDenied', 'UnrecognizedClientException'):
                    return 'DYNAMODB_ACCESS_DENIED', False
                # Validation / serialization
                if code in ('SerializationException', 'ValidationException'):
                    return 'DYNAMODB_SERIALIZATION_ERROR', False
                # Fallback
                return 'DYNAMODB_TRANSACTION_FAILED', False

            # Transport / connection-level exceptions
            nm = type(exc).__name__
            if EndpointConnectionError is not None and isinstance(exc, EndpointConnectionError):
                return 'DYNAMODB_UNAVAILABLE', True
            if ConnectTimeoutError is not None and isinstance(exc, ConnectTimeoutError):
                return 'DYNAMODB_TIMEOUT', True
            if ReadTimeoutError is not None and isinstance(exc, ReadTimeoutError):
                return 'DYNAMODB_TIMEOUT', True

            # Last-resort textual heuristics (conservative)
            msg = str(exc)
            # explicit transaction canceled token
            if 'TransactionCanceledException' in msg or 'TransactionCanceled' in msg:
                return 'DYNAMODB_TRANSACTION_CANCELED', False
            transient_tokens = ['Throttling', 'ProvisionedThroughputExceeded', 'RequestLimitExceeded', 'Timeout', 'timed out', 'InternalServerError', 'ServiceUnavailable', '500', '504', 'EndpointConnectionError']
            if any(tok in msg for tok in transient_tokens):
                return 'DYNAMODB_UNAVAILABLE', True
            if 'ConditionalCheckFailed' in msg:
                return 'DYNAMODB_CONDITIONAL_CONFLICT', False
            if 'AccessDenied' in msg:
                return 'DYNAMODB_ACCESS_DENIED', False
            if 'SerializationException' in msg or 'Serialization' in msg:
                return 'DYNAMODB_SERIALIZATION_ERROR', False
            return None, False

        attempt = 0
        last_exc: Optional[Exception] = None
        idempo_key = _extract_idempotency_key(transact_items)

        while attempt < max_attempts:
            try:
                client = self._client()
                return client.transact_write_items(TransactItems=transact_items)
            except AttributeError:
                # resource meta.client not present (fake resource); try calling on resource directly
                tbl = self._table()
                if hasattr(tbl, 'transact_write_items'):
                    try:
                        return tbl.transact_write_items(TransactItems=transact_items)
                    except Exception as e:
                        last_exc = e
                        code, is_transient = _classify_exception(e)
                        if code and not is_transient:
                            raise DynamoError(code) from e
                        # transient: check idempotency and possibly return success
                        if idempo_key:
                            existing = self.get_by_idempotency_key(idempo_key)
                            if existing:
                                return {'ExistingIdempotency': existing}
                        attempt += 1
                        if attempt >= max_attempts:
                            if code == 'DYNAMODB_THROTTLED':
                                raise DynamoError('DYNAMODB_THROTTLED') from e
                            if code == 'DYNAMODB_TIMEOUT':
                                raise DynamoError('DYNAMODB_TIMEOUT') from e
                            raise DynamoError('DYNAMODB_UNAVAILABLE') from e
                        backoff = base_backoff * (2 ** (attempt - 1))
                        time.sleep(backoff + random.random() * base_backoff)
                        continue
            except Exception as e:
                last_exc = e
                code, is_transient = _classify_exception(e)
                if code and not is_transient:
                    raise DynamoError(code) from e
                if is_transient:
                    # inspect idempotency before retrying
                    if idempo_key:
                        existing = self.get_by_idempotency_key(idempo_key)
                        if existing:
                            return {'ExistingIdempotency': existing}
                    attempt += 1
                    if attempt >= max_attempts:
                        if code == 'DYNAMODB_THROTTLED':
                            raise DynamoError('DYNAMODB_THROTTLED') from e
                        if code == 'DYNAMODB_TIMEOUT':
                            raise DynamoError('DYNAMODB_TIMEOUT') from e
                        raise DynamoError('DYNAMODB_UNAVAILABLE') from e
                    backoff = base_backoff * (2 ** (attempt - 1))
                    time.sleep(backoff + random.random() * base_backoff)
                    continue
                # fallback
                raise DynamoError('DYNAMODB_TRANSACTION_FAILED') from e

        # exhausted loop
        if last_exc is not None:
            raise DynamoError('DYNAMODB_UNAVAILABLE') from last_exc
        raise DynamoError('DYNAMODB_TRANSACTION_FAILED')

    def atomic_trade_commit(self, *, idempotency_key: str, idempotency_fingerprint: str, risk_id: str, risk_obj: Dict[str, Any], order_id: str, order_obj: Dict[str, Any], fill_id: str, fill_obj: Dict[str, Any], portfolio_id: str, new_portfolio_snapshot: Dict[str, Any], new_risk_state: Dict[str, Any], expected_risk_version: int, strategy_state_name: str | None = None, strategy_state: Dict[str, Any] | None = None):
        """Atomically commit a simulated trade: idempotency claim, risk decision, order, fill, portfolio snapshot, and risk state.

        Uses TransactWriteItems with condition checks for idempotency uniqueness and expected risk version.
        """
        # build transact items
        # Put idempotency item only if not exists
        transact_items: List[Dict[str, Any]] = []
        idempo_item = {'Put': {'TableName': self.table_name, 'Item': {'PK': {'S': f'IDEMPOTENCY#{idempotency_key}'}, 'SK': {'S': 'META'}, 'fingerprint': {'S': idempotency_fingerprint}, 'order_id': {'S': order_id}, 'result': {'S': json.dumps(self._to_decimal(order_obj), default=str)}} , 'ConditionExpression': 'attribute_not_exists(PK)'}}

        # Put risk decision
        risk_item = {'Put': {'TableName': self.table_name, 'Item': {'PK': {'S': f'RISK#{risk_id}'}, 'SK': {'S': 'META'}, 'payload': {'S': json.dumps(self._to_decimal(risk_obj), default=str)}}}}

        # Put order
        order_item = {'Put': {'TableName': self.table_name, 'Item': {'PK': {'S': f'ORDER#{order_id}'}, 'SK': {'S': 'META'}, 'payload': {'S': json.dumps(self._to_decimal(order_obj), default=str)}}}}

        # Put fill
        fill_item = {'Put': {'TableName': self.table_name, 'Item': {'PK': {'S': f'FILL#{fill_id}'}, 'SK': {'S': 'META'}, 'payload': {'S': json.dumps(self._to_decimal(fill_obj), default=str)}}}}

        # Put portfolio snapshot
        snap_item = {'Put': {'TableName': self.table_name, 'Item': {'PK': {'S': f'PORTFOLIO#{portfolio_id}'}, 'SK': {'S': f'SNAPSHOT#{datetime.now(timezone.utc).isoformat()}'}, 'snapshot': {'S': json.dumps(self._to_decimal(new_portfolio_snapshot), default=str)}}}}

        # Update risk state with condition on expected_version
        new_version = expected_risk_version + 1
        risk_state_item = {'Put': {'TableName': self.table_name, 'Item': {'PK': {'S': f'PORTFOLIO#{portfolio_id}'}, 'SK': {'S': 'RISK_STATE'}, 'state_json': {'S': json.dumps(self._to_decimal(new_risk_state), default=str)}, 'version': {'N': str(new_version)}}, 'ConditionExpression': 'version = :v', 'ExpressionAttributeValues': {':v': {'N': str(expected_risk_version)}}}}

        transact_items.extend([idempo_item, risk_item, order_item, fill_item, snap_item, risk_state_item])
        if strategy_state_name and strategy_state is not None:
            committed_strategy_state = dict(strategy_state)
            committed_strategy_state['last_trade_id'] = order_id
            transact_items.append({
                'Put': {
                    'TableName': self.table_name,
                    'Item': {
                        'PK': {'S': f'STRATEGY#{strategy_state_name}'},
                        'SK': {'S': 'STATE'},
                        'state_json': {'S': json.dumps(self._to_decimal(committed_strategy_state), default=str)},
                        'updated_at': {'S': datetime.now(timezone.utc).isoformat()},
                    },
                }
            })

        # execute
        try:
            self._transact_write(transact_items)
        except DynamoError:
            raise
        except Exception as e:
            raise DynamoError('DYNAMODB_TRANSACTION_FAILED') from e

        # success: return committed payload summary
        return {'order_id': order_id, 'fill_id': fill_id}
