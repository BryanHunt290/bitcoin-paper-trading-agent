import pytest
from src.market_data.adapter import get_market_data


def test_allowlist_rejects_non_btc():
    with pytest.raises(ValueError):
        get_market_data('ETH-USD', '15m', 10)


def test_malformed_ohlc_is_rejected(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [[1_700_000_000, 31_000, 30_000, 30_500, 30_600, 1.0]]

    monkeypatch.setattr('src.market_data.adapter.requests.get', lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match='inconsistent OHLC'):
        get_market_data('BTC-USD', '15m', 10)


def test_empty_market_response_is_rejected(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    monkeypatch.setattr('src.market_data.adapter.requests.get', lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match='no candle data'):
        get_market_data('BTC-USD', '15m', 10)
