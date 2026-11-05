"""AnthropicLLMProvider — Claude API implementation of LLMProvider.

`fast=True` selects LLM_MODEL_FAST (Haiku 4.5, for post-call classification
and summaries); the default selects LLM_MODEL_PRIMARY (Sonnet 5, for in-call
reasoning), per Phase 1 §5.1.

Model-tier note: Sonnet 5 runs adaptive thinking by default, which adds
latency we can't afford on the in-call path, so the primary path explicitly
disables it and sets a low effort. Haiku 4.5 does not support the
`thinking`/`output_config.effort` parameters at all — sending them returns a
400 — so the fast path omits them entirely rather than disabling them.
"""

from typing import Any

from anthropic import AsyncAnthropic, Timeout

from app.core.config import Settings
from app.modules.conversation.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
)


class AnthropicLLMProvider(LLMProvider):
    def __init__(self, settings: Settings) -> None:
        # `or None` lets the SDK fall back to its own credential resolution
        # (env var, OAuth profile) instead of hard-overriding with an empty
        # string when the setting is unconfigured.
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key or None,
            timeout=Timeout(settings.llm_timeout_seconds, connect=5.0),
            max_retries=settings.llm_max_retries,
        )
        self._primary_model = settings.llm_model_primary
        self._fast_model = settings.llm_model_fast

    async def complete(self, request: CompletionRequest, *, fast: bool = False) -> CompletionResult:
        model = self._fast_model if fast else self._primary_model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": request.messages,
        }
        if request.tools:
            kwargs["tools"] = request.tools
        if not fast:
            kwargs["thinking"] = {"type": "disabled"}
            kwargs["output_config"] = {"effort": "low"}

        response = await self._client.messages.create(**kwargs)

        text = "".join(block.text for block in response.content if block.type == "text")
        tool_calls = [
            {"id": block.id, "name": block.name, "input": block.input}
            for block in response.content
            if block.type == "tool_use"
        ]
        return CompletionResult(
            text=text,
            tool_calls=tool_calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
        )
