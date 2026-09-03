import json
import pytest
from datetime import datetime, timezone

from src.agent.config import BedrockConfig
from src.agent.bedrock_client import BedrockAgentModelProvider
from src.agent.bedrock_client import FakeAgentModelProvider
from src.agent.tool_registry import ToolRegistry
from tests.conftest import make_portfolio_and_broker


class MockClient:
    def __init__(self, responses=None, exc=None):
        self._responses = responses or []
        self._i = 0
        self._exc = exc
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        if self._i < len(self._responses):
            r = self._responses[self._i]
            self._i += 1
            return r
        return self._responses[-1]


def test_converse_final_decision_success():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    decision = {
        'symbol': 'BTC-USD', 'action': 'NO_TRADE', 'strategy_id': 'ema_cross_v1', 'confidence': 0.0, 'requested_notional_usd': 0.0, 'timestamp': datetime.now(timezone.utc).isoformat()
    }
    resp = {'output': {'message': {'role': 'assistant', 'content': [{'text': json.dumps(decision)}]}}}
    mock = MockClient(responses=[resp])
    prov = BedrockAgentModelProvider(cfg, tools, client=mock)
    ctx = {'messages': [{'role': 'user', 'text': 'Evaluate market'}]}
    d = prov.get_decision(ctx)
    assert d.action == 'NO_TRADE'
    call = mock.calls[0]
    assert call['modelId'] == 'test-model'
    assert call['system'][0]['text'] == prov.system_prompt()
    assert all(message['role'] in ('user', 'assistant') for message in call['messages'])
    assert call['toolConfig']['toolChoice'] == {'auto': {}}
    assert 'toolSpec' in call['toolConfig']['tools'][0]


def test_fenced_no_trade_json_is_normalized_safely():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    fenced = '''```json
{"symbol":"BTC-USD","action":"NO_TRADE","strategy_id":"invented","confidence":0.8,"requested_notional_usd":99,"timestamp":"2020-01-01T00:00:00Z"}
```'''
    response = {'output': {'message': {'role': 'assistant', 'content': [{'text': fenced}]}}}
    decision = BedrockAgentModelProvider(cfg, tools, client=MockClient([response])).get_decision(
        {'messages': [{'role': 'user', 'text': 'Analyze only'}]}
    )
    assert decision.action == 'NO_TRADE'
    assert decision.strategy_id == 'ema_cross_v1'
    assert decision.requested_notional_usd == 0.0
    assert decision.timestamp.year > 2020


def test_nova_thinking_prefix_and_decision_envelope_are_normalized_safely():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    text = (
        '<thinking>Concise internal analysis.</thinking>\n\n'
        '<thinking>No order should be submitted.</thinking>\n\n'
        '{"AgentDecision":{"symbol":"BTC-USD","action":"HOLD",'
        '"strategy_id":"N/A","confidence":"MEDIUM",'
        '"requested_notional_usd":25,"timestamp":"2020-01-01T00:00:00Z"}}'
    )
    response = {'output': {'message': {'role': 'assistant', 'content': [{'text': text}]}}}
    decision = BedrockAgentModelProvider(cfg, tools, client=MockClient([response])).get_decision(
        {'messages': [{'role': 'user', 'text': 'Analyze only'}]}
    )
    assert decision.action == 'HOLD'
    assert decision.symbol.value == 'BTC-USD'
    assert decision.strategy_id == 'ema_cross_v1'
    assert decision.confidence == 0.0
    assert decision.requested_notional_usd == 0.0


def test_arbitrary_prose_before_json_still_fails_closed():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    text = 'Here is the answer: {"symbol":"BTC-USD","action":"HOLD"}'
    response = {'output': {'message': {'role': 'assistant', 'content': [{'text': text}]}}}
    with pytest.raises(RuntimeError, match='BEDROCK_INVALID_RESPONSE'):
        BedrockAgentModelProvider(cfg, tools, client=MockClient([response])).get_decision(
            {'messages': [{'role': 'user', 'text': 'Analyze only'}]}
        )


def test_analysis_only_forces_structured_non_actionable_decision():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    tool_use = {
        'toolUse': {
            'toolUseId': 'decision-1',
            'name': 'return_analysis_decision',
            'input': {
                'symbol': 'BTC-USD',
                'action': 'HOLD',
                'strategy_id': 'model-invented',
                'confidence': 0.8,
                'requested_notional_usd': 0,
                'timestamp': '2020-01-01T00:00:00Z',
            },
        }
    }
    response = {'stopReason': 'tool_use', 'output': {'message': {'role': 'assistant', 'content': [tool_use]}}}
    mock = MockClient([response])
    decision = BedrockAgentModelProvider(
        cfg, tools, client=mock, analysis_only=True
    ).get_decision({'messages': [{'role': 'user', 'text': 'Analyze only'}]})
    assert decision.action == 'HOLD'
    assert decision.strategy_id == 'ema_cross_v1'
    assert decision.confidence == 0.0
    assert decision.requested_notional_usd == 0.0
    assert mock.calls[0]['toolConfig']['toolChoice'] == {
        'tool': {'name': 'return_analysis_decision'}
    }


def test_analysis_only_rejects_actionable_structured_decision():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    tool_use = {
        'toolUse': {
            'toolUseId': 'decision-1',
            'name': 'return_analysis_decision',
            'input': {
                'symbol': 'BTC-USD',
                'action': 'BUY',
                'strategy_id': 'ema_cross_v1',
                'confidence': 0.8,
                'requested_notional_usd': 10,
                'timestamp': '2020-01-01T00:00:00Z',
            },
        }
    }
    response = {'stopReason': 'tool_use', 'output': {'message': {'role': 'assistant', 'content': [tool_use]}}}
    with pytest.raises(RuntimeError, match='BEDROCK_INVALID_RESPONSE'):
        BedrockAgentModelProvider(
            cfg, tools, client=MockClient([response]), analysis_only=True
        ).get_decision({'messages': [{'role': 'user', 'text': 'Analyze only'}]})


def test_converse_tool_use_flow():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model', max_agent_iterations=3, max_tool_calls=3)
    # first response requests a tool
    tool_use = {'toolUse': {'toolUseId': 'tu1', 'name': 'get_portfolio', 'input': {'market_price': 30000}}}
    resp1 = {'output': {'message': {'role': 'assistant', 'content': [tool_use]}}}
    # second response returns final decision after tool result
    decision = {'symbol': 'BTC-USD', 'action': 'NO_TRADE', 'strategy_id': 'ema_cross_v1', 'confidence': 0.0, 'requested_notional_usd': 0.0, 'timestamp': datetime.now(timezone.utc).isoformat()}
    resp2 = {'output': {'message': {'role': 'assistant', 'content': [{'text': json.dumps(decision)}]}}}
    mock = MockClient(responses=[resp1, resp2])
    prov = BedrockAgentModelProvider(cfg, tools, client=mock)
    ctx = {'messages': [{'role': 'user', 'text': 'Get portfolio then decide'}]}
    d = prov.get_decision(ctx)
    assert d.action == 'NO_TRADE'


def test_unknown_tool_requested_fails_closed():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    tool_use = {'toolUse': {'toolUseId': 'tu1', 'name': 'nonexistent_tool', 'input': {}}}
    resp1 = {'output': {'message': {'role': 'assistant', 'content': [tool_use]}}}
    mock = MockClient(responses=[resp1])
    prov = BedrockAgentModelProvider(cfg, tools, client=mock)
    with pytest.raises(RuntimeError):
        prov.get_decision({'messages': [{'role': 'user', 'text': 'call unknown tool'}]})


def test_malformed_final_decision_fails_closed():
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    # model returns non-json text
    resp = {'output': {'message': {'role': 'assistant', 'content': [{'text': 'I think you should buy now'}]}}}
    mock = MockClient(responses=[resp])
    prov = BedrockAgentModelProvider(cfg, tools, client=mock)
    with pytest.raises(RuntimeError):
        prov.get_decision({'messages': [{'role': 'user', 'text': 'malformed final'}]})


def test_bedrock_timeout_raises_mapped_error():
    try:
        from botocore.exceptions import ReadTimeoutError
    except Exception:
        class ReadTimeoutError(Exception):
            def __init__(self, *args, **kwargs):
                super().__init__(*args)
    portfolio, broker = make_portfolio_and_broker()
    tools = ToolRegistry(portfolio, broker, store=None)
    cfg = BedrockConfig(bedrock_model_id='test-model')
    mock = MockClient(exc=ReadTimeoutError(endpoint_url='https://example'))
    prov = BedrockAgentModelProvider(cfg, tools, client=mock)
    with pytest.raises(RuntimeError):
        prov.get_decision({'messages': [{'role': 'user', 'text': 'timeout test'}]})
