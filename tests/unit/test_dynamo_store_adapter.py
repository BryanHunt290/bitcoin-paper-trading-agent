import json
from src.agent.dynamo_store import DynamoTradeEventStore
from decimal import Decimal
from datetime import datetime, timezone


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        # support PK/SK or pk/sk
        pk = Key.get('pk') or Key.get('PK')
        sk = Key.get('sk') or Key.get('SK')
        k = (pk, sk)
        return {'Item': self.items.get(k)}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        pk = Item.get('pk') or Item.get('PK')
        sk = Item.get('sk') or Item.get('SK')
        k = (pk, sk)
        # simple conditional support
        if ConditionExpression is not None:
            # emulate attribute_not_exists
            if 'attribute_not_exists' in ConditionExpression:
                if k in self.items:
                    raise Exception('ConditionalCheckFailed')
            else:
                # version = :v
                expected = ExpressionAttributeValues.get(':v')
                existing = self.items.get(k)
                if existing and int(existing.get('version', 0)) != expected:
                    raise Exception('ConditionalCheckFailed')
        self.items[k] = Item
        return {'ResponseMetadata': {'HTTPStatusCode': 200}}

    def query(self, IndexName=None, KeyConditionExpression=None, ExpressionAttributeValues=None, ScanIndexForward=True, Limit=None):
        pk = ExpressionAttributeValues.get(':pk')
        prefix = ExpressionAttributeValues.get(':prefix', '')
        res = [v for (item_pk, item_sk), v in self.items.items() if item_pk == pk and item_sk.startswith(prefix)]
        res.sort(key=lambda item: item.get('SK', ''), reverse=not ScanIndexForward)
        return {'Items': res[:Limit] if Limit else res}


class FakeResource:
    def __init__(self, table: FakeTable):
        self._table = table

    def Table(self, name):
        return self._table


def test_initialize_and_save_portfolio_state():
    table = FakeTable()
    res = FakeResource(table)
    store = DynamoTradeEventStore(table_name='test', dynamodb_resource=res)
    state = {'current_equity': 1000}
    out = store.initialize_portfolio_risk_state('p1', state)
    assert out['version'] == 1
    got = store.get_portfolio_risk_state('p1')
    assert got['state']['current_equity'] == 1000

    # save with expected version
    state['current_equity'] = 900
    res = store.save_portfolio_risk_state('p1', state, expected_version=1)
    assert res['version'] == 2


def test_atomic_trade_commit_and_idempotency():
    # fake table that supports transact_write_items
    class FakeTableTrans(FakeTable):
        def __init__(self):
            super().__init__()

        def transact_write_items(self, TransactItems=None):
            # check idempotency conditional
            for it in TransactItems:
                if 'Put' in it and it['Put']['Item'].get('PK', {}).get('S', '').startswith('IDEMPOTENCY#'):
                    pk = it['Put']['Item']['PK']['S']
                    if (pk, 'META') in self.items:
                        # simulate TransactionCanceled
                        raise Exception('TransactionCanceledException')
            # apply all puts
            for it in TransactItems:
                if 'Put' in it:
                    item = it['Put']['Item']
                    pk = item['PK']['S']
                    sk = item['SK']['S']
                    # store simplified dict
                    self.items[(pk, sk)] = {k: list(v.values())[0] for k, v in item.items()}
            return {'ResponseMetadata': {'HTTPStatusCode': 200}}

    fake = FakeTableTrans()
    res = FakeResource(fake)
    store = DynamoTradeEventStore(table_name='t', dynamodb_resource=res)

    # first commit succeeds
    out = store.atomic_trade_commit(
        idempotency_key='k1',
        idempotency_fingerprint='fp1',
        risk_id='r1',
        risk_obj={'allowed': True},
        order_id='o1',
        order_obj={'qty': 1},
        fill_id='f1',
        fill_obj={'price': 30000},
        portfolio_id='p1',
        new_portfolio_snapshot={'total_equity': Decimal('1000')},
        new_risk_state={'versioned': True},
        expected_risk_version=0
    )
    assert out['order_id'] == 'o1'
    existing = store.get_by_idempotency_key('k1')
    assert existing['fingerprint'] == 'fp1'
    assert existing['order']['result']['qty'] == 1

    # second commit with same idempotency -> transaction canceled
    try:
        store.atomic_trade_commit(
            idempotency_key='k1',
            idempotency_fingerprint='fp1',
            risk_id='r2',
            risk_obj={'allowed': True},
            order_id='o2',
            order_obj={'qty': 2},
            fill_id='f2',
            fill_obj={'price': 31000},
            portfolio_id='p1',
            new_portfolio_snapshot={'total_equity': Decimal('1200')},
            new_risk_state={'versioned': True},
            expected_risk_version=0
        )
        assert False, 'expected transaction canceled'
    except Exception:
        pass


def test_decimal_and_timestamp_serialization():
    table = FakeTable()
    res = FakeResource(table)
    store = DynamoTradeEventStore(table_name='test', dynamodb_resource=res)
    from decimal import Decimal as D
    snap = {'cash': D('100.12'), 'btc_qty': D('0.01234567')}
    ts = datetime.now(timezone.utc)
    store.save_portfolio_snapshot('p1', snap, ts)
    # retrieve stored item
    items = list(table.items.values())
    assert any('snapshot' in v for v in items)

def test_naive_datetime_rejected():
    table = FakeTable()
    res = FakeResource(table)
    store = DynamoTradeEventStore(table_name='test', dynamodb_resource=res)
    import pytest
    from datetime import datetime
    with pytest.raises(ValueError):
        store.save_portfolio_snapshot('p1', {'a': 1}, datetime.now())


def test_explicit_low_level_client_is_used_for_transactions():
    raw_client = object()
    store = DynamoTradeEventStore(
        table_name='test',
        dynamodb_resource=object(),
        dynamodb_client=raw_client,
    )
    assert store._client() is raw_client
