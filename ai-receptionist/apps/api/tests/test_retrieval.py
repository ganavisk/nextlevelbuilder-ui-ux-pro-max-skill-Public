"""Integration test: RAG retrieval against real Postgres + pgvector.

Uses the fake (deterministic bag-of-words) embedding provider so the test
doesn't need a live Voyage key, while still exercising the real thing this
phase actually adds: the vector column, the cosine_distance query, the HNSW
index, and tenant isolation on knowledge_chunks.
"""

import uuid

from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.db import tenant_session
from app.modules.conversation.knowledge import ingest_document
from app.modules.conversation.models import KnowledgeDoc
from app.modules.conversation.retrieval import retrieve_relevant_chunks
from tests.conftest import requires_services
from tests.fakes import FakeEmbeddingProvider


@requires_services
async def test_relevant_chunk_ranks_first_and_is_tenant_scoped() -> None:
    settings = Settings(_env_file=None, app_env="test")
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)
    embeddings = FakeEmbeddingProvider()

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    with engine.begin() as conn:
        for tid, slug in ((tenant_a, "rag-test-a"), (tenant_b, "rag-test-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, vertical) "
                    "VALUES (:id, :slug, 'RAG Test', 'dental')"
                ),
                {"id": str(tid), "slug": f"{slug}-{tid.hex[:6]}"},
            )

    try:
        # Tenant A: hours doc + insurance doc. Tenant B: an unrelated doc
        # with overlapping vocabulary, to prove RLS — not just ranking —
        # keeps it out of tenant A's results.
        async with tenant_session(tenant_a) as session:
            hours_doc = KnowledgeDoc(
                tenant_id=tenant_a,
                title="Hours",
                content="We are open Monday through Friday, closed on weekends.",
            )
            insurance_doc = KnowledgeDoc(
                tenant_id=tenant_a,
                title="Insurance",
                content="We accept Delta Dental and Cigna insurance plans for new patients.",
            )
            session.add_all([hours_doc, insurance_doc])
            await session.flush()
            hours_doc_id, insurance_doc_id = hours_doc.id, insurance_doc.id

        async with tenant_session(tenant_b) as session:
            # Same vocabulary as tenant A's hours doc, deliberately, so a
            # leak would rank this first instead of A's own doc — the RLS
            # policy, not just the ranking, is what must keep it out.
            other_doc = KnowledgeDoc(
                tenant_id=tenant_b,
                title="Other tenant hours",
                # Same vocabulary as tenant A's hours doc plus one marker word,
                # so a leaked chunk is identifiable in the results.
                content=(
                    "We are open Monday through Friday, closed on weekends. Tenantbmarker applies."
                ),
            )
            session.add(other_doc)
            await session.flush()
            other_doc_id = other_doc.id

        await ingest_document(tenant_a, hours_doc_id, embeddings)
        await ingest_document(tenant_a, insurance_doc_id, embeddings)
        await ingest_document(tenant_b, other_doc_id, embeddings)

        results = await retrieve_relevant_chunks(tenant_a, "Are you open on weekends?", embeddings)

        assert results, "expected at least one relevant chunk"
        assert results[0].content == hours_doc.content
        # The point of the test: tenant B's near-identical chunk must be
        # absent entirely, not merely ranked lower.
        assert not any("Tenantbmarker" in r.content for r in results)
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                {"a": str(tenant_a), "b": str(tenant_b)},
            )
        engine.dispose()


@requires_services
async def test_no_chunks_returns_empty_below_confidence_floor() -> None:
    settings = Settings(_env_file=None, app_env="test")
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)
    embeddings = FakeEmbeddingProvider()

    tenant_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical) "
                "VALUES (:id, :slug, 'Empty KB Test', 'dental')"
            ),
            {"id": str(tenant_id), "slug": f"empty-kb-{tenant_id.hex[:6]}"},
        )
    try:
        results = await retrieve_relevant_chunks(tenant_id, "anything at all", embeddings)
        assert results == []
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()
