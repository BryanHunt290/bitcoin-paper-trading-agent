import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError

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
    # small transact item just to exercise idempotency extraction path
    try:
        store._transact_write([{'Put': {'Item': {'PK': 'IDEMPOTENCY#k', 'SK': 'META'}}}])
    except DynamoError as de:
        return str(de)
    except Exception as e:
        return f'UNEXPECTED:{type(e).__name__}:{e}'
    return 'OK'


def test_conditional_check_failed_non_retryable():
    resp = {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'Conditional failed'}}
    err = ClientError(resp, 'TransactWriteItems')
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_CONDITIONAL_CONFLICT' in out


def test_transaction_canceled_inspects_cancellation_reasons():
    # CancellationReasons present pointing to idempotency item
    resp = {
        'Error': {'Code': 'TransactionCanceledException', 'Message': 'Transaction canceled'},
        'CancellationReasons': [
            {'Code': 'ConditionalCheckFailed', 'Item': {'PK': {'S': 'IDEMPOTENCY#abc'}}},
            {'Code': 'None'}
        ]
    }
    err = ClientError(resp, 'TransactWriteItems')
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_TRANSACTION_CANCELED' in out


def test_transaction_conflict_classified():
    resp = {'Error': {'Code': 'TransactionConflictException', 'Message': 'Conflict'}}
    err = ClientError(resp, 'TransactWriteItems')
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_CONDITIONAL_CONFLICT' in out


@pytest.mark.parametrize('code', ['ProvisionedThroughputExceededException', 'ThrottlingException', 'RequestLimitExceeded'])
def test_throttling_retryable(code):
    resp = {'Error': {'Code': code, 'Message': 'throttle'}}
    err = ClientError(resp, 'TransactWriteItems')
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_THROTTLED' in out


def test_internal_server_error_retryable():
    resp = {'Error': {'Code': 'InternalServerError', 'Message': 'internal'}}
    err = ClientError(resp, 'TransactWriteItems')
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_UNAVAILABLE' in out


@pytest.mark.parametrize('code', ['AccessDeniedException', 'UnrecognizedClientException'])
def test_access_and_unrecognized_non_retryable(code):
    resp = {'Error': {'Code': code, 'Message': 'access'}}
    err = ClientError(resp, 'TransactWriteItems')
    out = _call_classify_with_exception(err)
    assert 'DYNAMODB_ACCESS_DENIED' in out


def test_connect_timeout_maps_timeout():
    exc = ConnectTimeoutError(endpoint_url='https://dynamodb')
    out = _call_classify_with_exception(exc)
    assert 'DYNAMODB_TIMEOUT' in out or 'DYNAMODB_UNAVAILABLE' in out


def test_read_timeout_maps_timeout():
    exc = ReadTimeoutError(endpoint_url='https://dynamodb')
    out = _call_classify_with_exception(exc)
    assert 'DYNAMODB_TIMEOUT' in out or 'DYNAMODB_UNAVAILABLE' in out


def test_endpoint_connection_error_maps_unavailable():
    exc = EndpointConnectionError(endpoint_url='https://dynamodb')
    out = _call_classify_with_exception(exc)
    assert 'DYNAMODB_UNAVAILABLE' in out
