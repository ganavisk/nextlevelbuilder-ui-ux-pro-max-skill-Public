"""LLM/embedding provider factories — the single place adapter selection
happens. Constructed once at app startup (see main.py lifespan) and reused,
not per-request."""

from app.core.config import Settings
from app.modules.conversation.providers.anthropic_llm import AnthropicLLMProvider
from app.modules.conversation.providers.base import EmbeddingProvider, LLMProvider
from app.modules.conversation.providers.voyage import VoyageEmbeddingProvider


def get_llm_provider(settings: Settings) -> LLMProvider:
    return AnthropicLLMProvider(settings)


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    return VoyageEmbeddingProvider(settings)
