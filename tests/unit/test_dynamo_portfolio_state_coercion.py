import json

import pytest

from src.agent.dynamo_store import DynamoError, DynamoTradeEventStore


class _FakeTable:
    state = {
        'portfolio_id': 'default',
        'current_equity': '10000.0',
        'peak_equity': '10000.0',
        'day_start_equity': '10000.0',
        'trading_day_utc': '2026-09-02',
        'daily_pnl': '0.0',
        'current_drawdown': '0.0',
        'updated_at': '2026-09-02T00:00:00Z',
        'version': 1,
    }

    def get_item(self, Key=None):
        # Simulate persisted JSON where numeric fields were serialized as strings
        return {'Item': {'state_json': json.dumps(self.state), 'version': '1'}}


class _FakeResource:
    def Table(self, name):
        return _FakeTable()


def test_get_portfolio_risk_state_coerces_numbers():
    store = DynamoTradeEventStore(table_name='x', dynamodb_resource=_FakeResource())
    res = store.get_portfolio_risk_state('default')
    assert isinstance(res, dict)
    state = res.get('state')
    assert isinstance(state['current_equity'], float)
    assert isinstance(state['peak_equity'], float)
    assert isinstance(state['day_start_equity'], float)
    assert state['current_equity'] == 10000.0
    assert res.get('version') == 1


@pytest.mark.parametrize('invalid_value', ['not-a-number', 'NaN', 'Infinity'])
def test_get_portfolio_risk_state_rejects_invalid_numeric_state(invalid_value):
    class InvalidTable(_FakeTable):
        state = {**_FakeTable.state, 'day_start_equity': invalid_value}

    class InvalidResource:
        def Table(self, name):
            return InvalidTable()

    store = DynamoTradeEventStore(table_name='x', dynamodb_resource=InvalidResource())
    with pytest.raises(DynamoError, match='DYNAMODB_SERIALIZATION_ERROR'):
        store.get_portfolio_risk_state('default')
