import pytest
from decimal import Decimal
from datetime import datetime, timezone
from src.agent.dynamo_store import DynamoTradeEventStore, DynamoError
from tests.unit.test_dynamo_store_adapter import FakeTable, FakeResource


class FakeTableThrottling(FakeTable):
    def __init__(self, fail_times=2):
        super().__init__()
        self.fail_times = fail_times
        self.calls = 0

    def transact_write_items(self, TransactItems=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise Exception('Throttling')
        # on success, store items as in adapter tests
        for it in TransactItems:
            if 'Put' in it:
                item = it['Put']['Item']
                pk = item['PK']['S']
                sk = item['SK']['S']
                self.items[(pk, sk)] = {k: list(v.values())[0] for k, v in item.items()}
        return {'ResponseMetadata': {'HTTPStatusCode': 200}}


class FakeTableAlwaysThrottling(FakeTableThrottling):
    def __init__(self):
        super().__init__(fail_times=9999)


def make_store_with_table(table):
    res = FakeResource(table)
    return DynamoTradeEventStore(table_name='t', dynamodb_resource=res), table


def commit_sample(store):
    return store.atomic_trade_commit(
        idempotency_key='retryk',
        idempotency_fingerprint='fp',
        risk_id='r1',
        risk_obj={'ok': True},
        order_id='o1',
        order_obj={'qty': 1},
        fill_id='f1',
        fill_obj={'price': 100},
        portfolio_id='p1',
        new_portfolio_snapshot={'total_equity': Decimal('1000')},
        new_risk_state={'v': True},
        expected_risk_version=0
    )


def test_throttling_retries_and_succeeds():
    table = FakeTableThrottling(fail_times=2)
    store, table = make_store_with_table(table)
    out = commit_sample(store)
    assert out['order_id'] == 'o1'


def test_throttling_exhausts_and_fails_closed():
    table = FakeTableAlwaysThrottling()
    store, table = make_store_with_table(table)
    with pytest.raises(DynamoError) as e:
        commit_sample(store)
    assert 'DYNAMODB_THROTTLED' in str(e.value) or 'DYNAMODB_UNAVAILABLE' in str(e.value)


def test_conditional_conflict_not_retried():
    class FakeCond(FakeTable):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def transact_write_items(self, TransactItems=None):
            self.calls += 1
            raise Exception('ConditionalCheckFailed')

    table = FakeCond()
    store, table = make_store_with_table(table)
    with pytest.raises(DynamoError) as e:
        commit_sample(store)
    assert 'DYNAMODB_CONDITIONAL_CONFLICT' in str(e.value)


def test_access_denied_not_retried():
    class FakeAccess(FakeTable):
        def transact_write_items(self, TransactItems=None):
            raise Exception('AccessDeniedException')

    table = FakeAccess()
    store, table = make_store_with_table(table)
    with pytest.raises(DynamoError) as e:
        commit_sample(store)
    assert 'DYNAMODB_ACCESS_DENIED' in str(e.value)


def test_transaction_canceled_maps_correctly():
    class FakeTxCancelled(FakeTable):
        def transact_write_items(self, TransactItems=None):
            raise Exception('TransactionCanceledException')

    table = FakeTxCancelled()
    store, table = make_store_with_table(table)
    with pytest.raises(DynamoError) as e:
        commit_sample(store)
    assert 'DYNAMODB_TRANSACTION_CANCELED' in str(e.value)


def test_retry_does_not_create_duplicate_fill():
    table = FakeTableThrottling(fail_times=1)
    store, table = make_store_with_table(table)
    out = commit_sample(store)
    # ensure single fill exists
    fills = [k for k in table.items.keys() if k[0].startswith('FILL#')]
    assert len(fills) == 1


def test_persistence_uncertainty_prevents_new_buy_exposure():
    # always throttling should fail and not store any ORDER/FILL
    table = FakeTableAlwaysThrottling()
    store, table = make_store_with_table(table)
    with pytest.raises(DynamoError):
        commit_sample(store)
    orders = [k for k in table.items.keys() if k[0].startswith('ORDER#')]
    fills = [k for k in table.items.keys() if k[0].startswith('FILL#')]
    assert len(orders) == 0 and len(fills) == 0
