import pytest
from botocore.exceptions import ClientError

from src.agent.dynamo_store import DynamoTradeEventStore, DynamoError


class FakeTableRaise:
    def __init__(self, exc):
        self._exc = exc

    def transact_write_items(self, TransactItems=None):
        raise self._exc
    def get_item(self, Key=None):
        return {}
    def put_item(self, Item=None, ConditionExpression=None, ExpressionAttributeValues=None):
        return {'ResponseMetadata': {'HTTPStatusCode': 200}}


class FakeResource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):
        return self._table


def _call_classify_with_exception(exc):
    tbl = FakeTableRaise(exc)
    res = FakeResource(tbl)
    store = DynamoTradeEventStore(dynamodb_resource=res)
    try:
        store._transact_write([{'Put': {'Item': {'PK': 'IDEMPOTENCY#k', 'SK': 'META'}}}])
    except DynamoError as de:
        return str(de)
    except Exception as e:
        return f'UNEXPECTED:{type(e).__name__}:{e}'
    return 'OK'


def make_tx_cancel_error(reasons):
    resp = {'Error': {'Code': 'TransactionCanceledException', 'Message': 'Transaction canceled'}, 'CancellationReasons': reasons}
    return ClientError(resp, 'TransactWriteItems')


def test_idempotency_conditional_identified():
    reasons = [
        {'Code': 'ConditionalCheckFailed'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
    ]
    err = make_tx_cancel_error(reasons)
    out = _call_classify_with_exception(err)
    assert 'IDEMPOTENCY_CONFLICT' in out


def test_risk_state_conditional_identified():
    reasons = [
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'ConditionalCheckFailed'},
    ]
    err = make_tx_cancel_error(reasons)
    out = _call_classify_with_exception(err)
    assert 'RISK_STATE_CONFLICT' in out


def test_other_conditional_on_order_maps_generic():
    reasons = [
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'ConditionalCheckFailed'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
    ]
    err = make_tx_cancel_error(reasons)
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_CONDITIONAL_CONFLICT' in out


def test_multiple_conditional_failures_are_ambiguous():
    reasons = [
        {'Code': 'ConditionalCheckFailed'},
        {'Code': 'None'},
        {'Code': 'ConditionalCheckFailed'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
    ]
    err = make_tx_cancel_error(reasons)
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_TRANSACTION_CANCELED' in out


def test_missing_cancellation_reasons_are_ambiguous():
    resp = {'Error': {'Code': 'TransactionCanceledException', 'Message': 'Transaction canceled'}}
    err = ClientError(resp, 'TransactWriteItems')
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_TRANSACTION_CANCELED' in out


def test_non_conditional_reason_is_ambiguous():
    reasons = [
        {'Code': 'ConditionalCheckFailed'},
        {'Code': 'None'},
        {'Code': 'ProvisionedThroughputExceededException'},
        {'Code': 'None'},
        {'Code': 'None'},
        {'Code': 'None'},
    ]
    err = make_tx_cancel_error(reasons)
    out = _call_classify_with_exception(err)
    # because a non-conditional failure exists alongside the conditional one, treat as ambiguous
    assert 'DYNAMODB_TRANSACTION_CANCELED' in out
