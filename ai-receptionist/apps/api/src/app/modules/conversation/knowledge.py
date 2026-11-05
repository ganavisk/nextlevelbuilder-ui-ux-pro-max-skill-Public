"""Knowledge-base ingestion: chunk tenant documents and embed them for RAG.

Chunking is a fixed-size sliding window over raw text — simple and sufficient
for FAQ-style content (services, hours, policies). Revisit with a
sentence/paragraph-aware splitter if tenants start uploading long-form
documents.
"""

import uuid

from sqlalchemy import delete

from app.core.db import tenant_session
from app.core.errors import NotFoundError
from app.modules.conversation.models import KnowledgeChunk, KnowledgeDoc
from app.modules.conversation.providers.base import EmbeddingProvider

CHUNK_SIZE_CHARS = 800
CHUNK_OVERLAP_CHARS = 150


def chunk_text(
    text: str, *, size: int = CHUNK_SIZE_CHARS, overlap: int = CHUNK_OVERLAP_CHARS
) -> list[str]:
    """Split text into overlapping fixed-size windows. `overlap` must be
    smaller than `size` or the window never advances."""
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end - overlap
    return chunks


async def ingest_document(
    tenant_id: uuid.UUID, doc_id: uuid.UUID, embeddings: EmbeddingProvider
) -> int:
    """(Re)chunk and (re)embed a document. Idempotent: replaces prior chunks
    for the doc. Returns the number of chunks created."""
    async with tenant_session(tenant_id) as session:
        doc = await session.get(KnowledgeDoc, doc_id)
        if doc is None:
            raise NotFoundError(f"knowledge doc {doc_id} not found for tenant {tenant_id}")

        pieces = chunk_text(doc.content)
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id))

        if not pieces:
            doc.status = "failed"
            return 0

        vectors = await embeddings.embed(pieces, for_query=False)
        for index, (content, vector) in enumerate(zip(pieces, vectors, strict=True)):
            session.add(
                KnowledgeChunk(
                    tenant_id=tenant_id,
                    doc_id=doc_id,
                    chunk_index=index,
                    content=content,
                    embedding=vector,
                )
            )
        doc.status = "indexed"

    return len(pieces)
