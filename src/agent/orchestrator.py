from __future__ import annotations
from typing import Any, Dict, Optional
from .bedrock_client import AgentModelProvider
from .tool_registry import ToolRegistry, UnknownToolError
from .models import AgentDecision
import logging

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    def __init__(self, provider: AgentModelProvider, tools: ToolRegistry, max_iterations: int = 3, max_tool_calls: int = 10):
        self.provider = provider
        self.tools = tools
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls

    def run(self, context: Dict[str, Any]) -> AgentDecision:
        tool_calls = 0
        for iteration in range(self.max_iterations):
            decision = self.provider.get_decision(context)
            # validate decision structure via pydantic in provider
            if not isinstance(decision, AgentDecision):
                # malformed model output: log and continue until max_iterations reached
                logger.warning('Malformed model output received; iteration=%d', iteration)
                continue
            # if decision is final (NO_TRADE/HOLD/BUY/SELL), optionally call submit
            if decision.action in ('BUY', 'SELL'):
                # ensure limited tool calls
                if tool_calls >= self.max_tool_calls:
                    raise RuntimeError('Tool call limit exceeded')
                # fetch market snapshot
                market = self.tools.get_market_data('BTC-USD', '15m', 10)
                # submit via allowlisted tool
                try:
                    result = self.tools.submit_paper_order(decision, market)
                    logger.info('Order submitted: %s', result)
                except Exception as e:
                    logger.warning('Submission rejected: %s', e)
                return decision
            # if HOLD/NO_TRADE return now
            if decision.action in ('HOLD', 'NO_TRADE'):
                return decision
        raise RuntimeError('Max iterations reached without final decision')
