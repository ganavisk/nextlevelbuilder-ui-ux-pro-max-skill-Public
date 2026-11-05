"""VoyageEmbeddingProvider — Voyage AI implementation of EmbeddingProvider.

Voyage's embedding models are asymmetric: documents and search queries are
embedded differently for best retrieval quality, selected here via
`input_type`.

Not yet live-tested against the Voyage API (no sandbox key in this
environment) — verify the exact client/method signature against
https://docs.voyageai.com when VOYAGE_API_KEY is available, the same way the
Vapi adapter's payload shapes were validated against fixtures first and the
live sandbox second.
"""

from typing import cast

import voyageai

from app.core.config import Settings
from app.modules.conversation.providers.base import EmbeddingProvider


class VoyageEmbeddingProvider(EmbeddingProvider):
    def __init__(self, settings: Settings) -> None:
        self._client = voyageai.AsyncClient(  # type: ignore[attr-defined]
            api_key=settings.voyage_api_key or None,
            max_retries=settings.embedding_max_retries,
            timeout=settings.embedding_timeout_seconds,
        )
        self._model = settings.embedding_model

    async def embed(self, texts: list[str], *, for_query: bool = False) -> list[list[float]]:
        result = await self._client.embed(
            texts,
            model=self._model,
            input_type="query" if for_query else "document",
        )
        return cast(list[list[float]], result.embeddings)
