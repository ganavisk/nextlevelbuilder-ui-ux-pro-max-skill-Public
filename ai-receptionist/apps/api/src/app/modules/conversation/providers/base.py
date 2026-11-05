"""LLMProvider and EmbeddingProvider — model access for the conversation
engine (Claude today) and for knowledge-base retrieval (Voyage today).

The conversation module owns prompt assembly, RAG retrieval, and lead
qualification; this interface only moves text and vectors, so either vendor
is swappable independently.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CompletionRequest:
    system: str
    messages: list[dict[str, Any]]
    max_tokens: int = 1024
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CompletionResult:
    text: str
    tool_calls: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    model: str


class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, request: CompletionRequest, *, fast: bool = False) -> CompletionResult:
        """Run a completion. `fast=True` selects the cheap/low-latency model
        (classification, summaries); default is the primary in-call model."""


class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str], *, for_query: bool = False) -> list[list[float]]:
        """Embed document chunks (default) or a search query (for_query)."""
