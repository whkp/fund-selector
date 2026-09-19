"""Portable Pi-compatible agent harness primitives for Tau.

Vendored from https://github.com/huggingface/tau (commit 9fe6a71, MIT
License — see LICENSE). Vendored subset: harness / loop / tools / messages /
events. The session persistence layer (tau_agent.session) is intentionally
NOT vendored — fund-compass persists conversations in Postgres instead.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from tau_agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from tau_agent.harness import (
    AgentHarness,
    AgentHarnessConfig,
    EventListener,
    QueuedMessages,
    SimpleCancellationToken,
)
from tau_agent.loop import run_agent_loop
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    ImageContent,
    ResponseTiming,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    content_text,
    message_text,
)
from tau_agent.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolExecutionMode,
    ToolExecutor,
    ToolUpdateCallback,
)
from tau_agent.types import JSONObject, JSONPrimitive, JSONValue

__all__ = [name for name in globals() if not name.startswith("_")]
