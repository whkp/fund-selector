"""Provider and Pi-compatible model streaming layer for Tau.

Vendored from https://github.com/huggingface/tau (commit 9fe6a71, MIT
License — see LICENSE). Vendored subset: the OpenAI-compatible provider and
its dependencies only. Anthropic / Google / Mistral / OpenAI-Codex providers
are intentionally NOT vendored — fund-compass talks to OpenAI-compatible
endpoints (dashscope compatible-mode / ollama / vLLM).
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from tau_ai.env import (
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    OpenAICompatibleConfig,
    RuntimeProviderAuth,
)
from tau_ai.events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from tau_ai.fake import FakeProvider
from tau_ai.openai_compatible import OpenAICompatibleProvider
from tau_ai.provider import CancellationToken, ModelProvider

__all__ = [name for name in globals() if not name.startswith("_")]
