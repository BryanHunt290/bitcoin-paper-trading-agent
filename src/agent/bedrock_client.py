from __future__ import annotations
from typing import Any, Dict, Optional, List
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from .models import AgentDecision
from .config import BedrockConfig
from .tool_registry import ToolRegistry, UnknownToolError
import json
import logging

logger = logging.getLogger(__name__)


class AgentModelProvider(ABC):
    @abstractmethod
    def system_prompt(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def get_decision(self, context: Dict[str, Any]) -> AgentDecision:
        raise NotImplementedError()


class FakeAgentModelProvider(AgentModelProvider):
    """A deterministic, offline model provider for tests.

    It returns preconfigured AgentDecision objects or uses a simple rule.
    """
    def __init__(self, decision: Optional[Dict[str, Any]] = None):
        self._decision = decision

    def system_prompt(self) -> str:
        return "You are a Bitcoin trading research and paper-trading agent. Use tools."

    def get_decision(self, context: Dict[str, Any]) -> AgentDecision:
        now = datetime.now(timezone.utc)
        if self._decision:
            data = dict(self._decision)
            data.setdefault('timestamp', now)
            return AgentDecision(**data)
        # default: NO_TRADE
        return AgentDecision(symbol='BTC-USD', action='NO_TRADE', strategy_id='ema_cross_v1', confidence=0.0, requested_notional_usd=0.0, timestamp=now)


class BedrockAgentModelProvider(AgentModelProvider):
    """Production provider that calls Amazon Bedrock Runtime using the Converse API.

    - Uses `BedrockConfig` for all timeouts/limits.
    - Accepts a `ToolRegistry` to validate/execute tool calls.
    - Accepts an optional boto3 client for testing to avoid network calls.
    - Strictly parses model responses and fails closed on malformed output.
    """
    def __init__(self, config: BedrockConfig, tools: ToolRegistry, client: Optional[Any] = None, analysis_only: bool = False):
        # Defer importing boto3/botocore until we actually need a real AWS client.
        # Tests should pass an injected `client` (mock) to avoid requiring boto3 in CI.
        self.config = config
        self.tools = tools
        self._external_client = client
        if client is None:
            try:
                import boto3
                from botocore.config import Config as BotocoreConfig
            except Exception:
                raise RuntimeError('boto3 and botocore are required for BedrockAgentModelProvider')
            botocore_cfg = BotocoreConfig(
                connect_timeout=self.config.bedrock_connect_timeout_seconds,
                read_timeout=self.config.bedrock_read_timeout_seconds,
                retries={'max_attempts': self.config.bedrock_max_attempts, 'mode': 'standard'},
            )
            self._client = boto3.client('bedrock-runtime', region_name=self.config.aws_region, config=botocore_cfg)
        else:
            self._client = client
        self._analysis_only = analysis_only
        if analysis_only:
            decision_schema = {
                'type': 'object',
                'properties': {
                    'symbol': {'type': 'string', 'enum': ['BTC-USD']},
                    'action': {'type': 'string', 'enum': ['HOLD', 'NO_TRADE']},
                    'strategy_id': {'type': 'string'},
                    'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
                    'requested_notional_usd': {'type': 'number', 'enum': [0]},
                    'timestamp': {'type': 'string'},
                },
                'required': [
                    'symbol', 'action', 'strategy_id', 'confidence',
                    'requested_notional_usd', 'timestamp',
                ],
                'additionalProperties': False,
            }
            self._tool_specs = [{
                'toolSpec': {
                    'name': 'return_analysis_decision',
                    'description': 'Return the final BTC-USD paper-only research decision.',
                    'inputSchema': {'json': decision_schema},
                }
            }]
        else:
            # pre-build tool specs from registry
            self._tool_specs = self._build_tool_specs(list(self.tools.tools.keys()))

    def system_prompt(self) -> str:
        return (
            "You are a Bitcoin research and paper-trading agent. Use only approved tools. "
            "Never fabricate prices or request secrets. BTC-USD only; no leverage, margin, "
            "shorting, or real orders. Return only one JSON object matching AgentDecision "
            "with symbol, action, strategy_id, confidence, requested_notional_usd, and "
            "timezone-aware timestamp."
        )

    def _build_tool_specs(self, names: List[str]) -> List[Dict[str, Any]]:
        # Map allowed tool names to minimal JSON Schema-like input schemas.
        specs = []
        for n in names:
            if n == 'get_market_data':
                schema = {
                    'type': 'object',
                    'properties': {
                        'symbol': {'type': 'string'},
                        'timeframe': {'type': 'string'},
                        'candle_count': {'type': 'integer'}
                    },
                    'required': ['symbol', 'timeframe', 'candle_count'],
                    'additionalProperties': False,
                }
            elif n == 'calculate_indicators':
                schema = {'type': 'object', 'properties': {'candles': {'type': 'array'}}, 'required': ['candles'], 'additionalProperties': False}
            elif n == 'get_portfolio':
                schema = {'type': 'object', 'properties': {'market_price': {'type': 'number'}}, 'required': ['market_price'], 'additionalProperties': False}
            elif n == 'get_strategy_performance':
                schema = {'type': 'object', 'properties': {'strategy_id': {'type': 'string'}}, 'required': ['strategy_id'], 'additionalProperties': False}
            elif n == 'run_backtest':
                schema = {'type': 'object', 'properties': {'strategy_id': {'type': 'string'}, 'historical_candles': {'type': 'array'}}, 'required': ['strategy_id','historical_candles'], 'additionalProperties': False}
            elif n == 'analyze_previous_trades':
                schema = {'type': 'object', 'properties': {'trades': {'type': 'array'}}, 'required': ['trades'], 'additionalProperties': False}
            elif n == 'submit_paper_order':
                schema = {'type': 'object', 'properties': {'proposal': {'type': 'object'}, 'market_snapshot': {'type': 'object'}}, 'required': ['proposal','market_snapshot'], 'additionalProperties': False}
            elif n == 'explain_trade':
                schema = {'type': 'object', 'properties': {'decision': {'type': 'object'}, 'market_snapshot': {'type': 'object'}}, 'required': ['decision','market_snapshot'], 'additionalProperties': False}
            else:
                continue
            specs.append({'toolSpec': {'name': n, 'description': f'Approved tool: {n}', 'inputSchema': {'json': schema}}})
        return specs

    def _converse(self, system: str, messages: List[Dict[str, Any]], tool_config: Dict[str, Any], prior_tool_results: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        # Use Converse API
        payload = {
            'modelId': self.config.bedrock_model_id,
            'system': [{'text': system}],
            'messages': messages,
            'toolConfig': tool_config,
        }
        # include prior tool results if provided as part of request metadata
        try:
            resp = self._client.converse(**payload)
            return resp
        except Exception as e:
            # translate into structured provider errors
            err_type = type(e).__name__
            code = 'BEDROCK_UNKNOWN'
            msg = str(e)
            if 'Timeout' in err_type or 'ReadTimeout' in err_type or 'ConnectTimeout' in err_type:
                code = 'BEDROCK_TIMEOUT'
            elif 'Throttling' in msg or 'Throttled' in msg:
                code = 'BEDROCK_THROTTLED'
            elif 'AccessDenied' in msg:
                code = 'BEDROCK_ACCESS_DENIED'
            elif 'Unauthorized' in msg or 'Auth' in msg:
                code = 'BEDROCK_AUTH_FAILURE'
            logger.warning('Bedrock converse failed: %s', code)
            raise RuntimeError(code)

    def get_decision(self, context: Dict[str, Any]) -> AgentDecision:
        # Orchestrate a bounded tool-call loop using the Bedrock Converse API.
        system = self.system_prompt()
        messages = context.get('messages', [])
        # ensure messages are in required format: list of {role, content:[{text:str}]}
        safe_messages = []
        for m in messages:
            if isinstance(m, dict) and m.get('role') in ('user', 'assistant') and isinstance(m.get('text'), str):
                safe_messages.append({'role': m['role'], 'content': [{'text': m['text']}]})
        if not safe_messages:
            safe_messages.append({'role': 'user', 'content': [{'text': 'Analyze BTC-USD and return HOLD or NO_TRADE.'}]})

        tool_choice = (
            {'tool': {'name': 'return_analysis_decision'}}
            if self._analysis_only else {'auto': {}}
        )
        tool_config = {'tools': self._tool_specs, 'toolChoice': tool_choice}

        tool_calls = 0
        iterations = 0
        last_response = None
        while iterations < self.config.max_agent_iterations and tool_calls < self.config.max_tool_calls:
            iterations += 1
            resp = self._converse(system, safe_messages, tool_config)
            last_response = resp
            # parse stopReason
            stop_reason = resp.get('stopReason')
            # parse output message blocks
            out = resp.get('output') or {}
            message = out.get('message') if out else None
            contents = message.get('content') if message else []
            # iterate content blocks
            handled_tool = False
            for block in contents:
                if 'toolUse' in block:
                    tool_use = block['toolUse']
                    tool_name = tool_use.get('name')
                    tool_use_id = tool_use.get('toolUseId')
                    tool_input = tool_use.get('input') or {}
                    if self._analysis_only and tool_name == 'return_analysis_decision':
                        try:
                            parsed = dict(tool_input)
                            if parsed.get('action') not in ('HOLD', 'NO_TRADE'):
                                raise ValueError('actionable analysis decision')
                            parsed['timestamp'] = datetime.now(timezone.utc)
                            parsed['strategy_id'] = 'ema_cross_v1'
                            parsed['confidence'] = 0.0
                            parsed['requested_notional_usd'] = 0.0
                            return AgentDecision(**parsed)
                        except Exception:
                            logger.warning('Malformed structured analysis decision from model')
                            raise RuntimeError('BEDROCK_INVALID_RESPONSE')
                    # validate tool exists
                    if tool_name not in self.tools.tools:
                        logger.warning('Model requested unknown tool: %s', tool_name)
                        raise RuntimeError('BEDROCK_INVALID_RESPONSE')
                    # run tool (validated through registry)
                    try:
                        result = self.tools.call(tool_name, **tool_input)
                    except UnknownToolError:
                        logger.warning('Unknown tool requested: %s', tool_name)
                        raise RuntimeError('BEDROCK_INVALID_RESPONSE')
                    except Exception as e:
                        # tool failed: fail closed
                        logger.warning('Tool execution failed: %s', e)
                        raise RuntimeError('TOOL_EXECUTION_FAILED')
                    # serialize tool result into a toolResult content block and append to messages
                    safe_messages.append(message)
                    tool_result_block = {'toolResult': {'toolUseId': tool_use_id, 'status': 'success', 'content': [{'text': json.dumps(result, default=str)}]}}
                    safe_messages.append({'role': 'user', 'content': [tool_result_block]})
                    tool_calls += 1
                    handled_tool = True
                    break
                elif 'text' in block:
                    txt = block.get('text')
                    # try parse JSON into AgentDecision
                    try:
                        candidate = txt.strip()
                        # Nova may prepend one or more internal <thinking> blocks even
                        # when instructed to return JSON only. Discard only a complete
                        # leading thinking section; arbitrary prose still fails closed.
                        if candidate.startswith('<thinking>'):
                            thinking_end = candidate.rfind('</thinking>')
                            if thinking_end < 0:
                                raise ValueError('unterminated thinking block')
                            candidate = candidate[thinking_end + len('</thinking>'):].strip()
                        if candidate.startswith('```'):
                            lines = candidate.splitlines()
                            if len(lines) < 3 or lines[-1].strip() != '```':
                                raise ValueError('malformed fenced JSON')
                            candidate = '\n'.join(lines[1:-1]).strip()
                        parsed = json.loads(candidate)
                        if not isinstance(parsed, dict):
                            raise ValueError('decision must be an object')
                        # Nova also sometimes uses a single documented-style envelope.
                        # Accept that exact envelope only; extra sibling content is
                        # rejected instead of being silently ignored.
                        if set(parsed) == {'AgentDecision'} and isinstance(parsed['AgentDecision'], dict):
                            parsed = parsed['AgentDecision']
                        # The runtime, not the model, is authoritative for time.
                        parsed['timestamp'] = datetime.now(timezone.utc)
                        if parsed.get('action') in ('HOLD', 'NO_TRADE'):
                            parsed['strategy_id'] = 'ema_cross_v1'
                            parsed['requested_notional_usd'] = 0.0
                            parsed['confidence'] = 0.0
                        elif parsed.get('strategy_id') != 'ema_cross_v1':
                            raise ValueError('unsupported strategy')
                        # enforce no extra fields by using pydantic model
                        decision = AgentDecision(**parsed)
                        return decision
                    except Exception:
                        logger.warning('Malformed final decision from model')
                        raise RuntimeError('BEDROCK_INVALID_RESPONSE')
            # if a tool was handled, continue loop to send result back
            if handled_tool:
                continue
            # no actionable content — treat as NO_TRADE
            logger.info('No actionable model output; returning NO_TRADE')
            now = datetime.now(timezone.utc)
            return AgentDecision(symbol='BTC-USD', action='NO_TRADE', strategy_id='ema_cross_v1', confidence=0.0, requested_notional_usd=0.0, timestamp=now)
        # reached limits
        logger.warning('Bedrock provider reached iteration/tool limits')
        raise RuntimeError('BEDROCK_MAX_ITERATIONS')
