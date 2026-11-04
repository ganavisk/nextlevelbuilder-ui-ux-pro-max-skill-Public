"""Indexes for the hot paths, and tenant-first knowledge retrieval.

From the Phase 8 database review (docs/ai-receptionist/PHASE-8-PRODUCTION.md,
section 3). Each index serves a measured or identified access path:

- appointments (tenant_id, ends_at) WHERE confirmed: availability, run on
  every check_availability while a caller waits. Past bookings stay
  'confirmed', so without it the query read a tenant's whole history.
  Measured with 30,000 past bookings: 8.1 ms to 0.12 ms, 2,003 to 7 buffers.
- appointments (tenant_id, starts_at DESC): the Appointments page.
- call_events (tenant_id, occurred_at DESC): the Test Lab activity feed,
  polled every 3 s, on the table every webhook writes to.
- callback_requests (tenant_id, (status = 'resolved'), created_at DESC): the
  callback queue, which sorts open before resolved, then newest first. A
  plain (tenant_id, created_at) index can't supply that order: measured, the
  first page read and sorted every callback.
- knowledge_chunks (tenant_id): retrieval now filters to the tenant first.
- call_id on five tables, partial (IS NOT NULL): Test Lab discard deletes by
  call_id, and deleting a call sets these to NULL. Without an index each is
  a sequential scan.

And one drop: the global HNSW index on knowledge_chunks.embedding. With row-
level security it searched every tenant's chunks and filtered afterwards, so
once another tenant's chunks filled its candidate list (ef_search, 40) a
tenant could get none of its own back. That was reproduced with 20,000
chunks: 0 rows for a tenant whose relevant chunk was at distance 0.011.
Retrieval now filters to the tenant and computes exact distances (each
tenant's knowledge base is small), so the index is unused write and memory
cost. If a tenant ever has tens of thousands of chunks, use pgvector >= 0.8
iterative scans rather than restoring a global index.

Built CONCURRENTLY so production writes aren't blocked while indexes build.
That can't run in a transaction, hence autocommit_block. If a concurrent
build fails it leaves an INVALID index; drop it and re-run (IF NOT EXISTS
would otherwise skip it).

Revision ID: 0012
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

INDEXES = {
    "appointments_upcoming_idx": ("appointments (tenant_id, ends_at) WHERE status = 'confirmed'"),
    "appointments_tenant_starts_idx": "appointments (tenant_id, starts_at DESC)",
    "call_events_tenant_occurred_idx": "call_events (tenant_id, occurred_at DESC)",
    # Matches the queue's ORDER BY exactly (open before resolved, then newest),
    # so the first page is read straight off the index.
    "callback_requests_queue_idx": (
        "callback_requests (tenant_id, (status = 'resolved'), created_at DESC)"
    ),
    "knowledge_chunks_tenant_idx": "knowledge_chunks (tenant_id)",
    "appointments_call_idx": "appointments (call_id) WHERE call_id IS NOT NULL",
    "callback_requests_call_idx": "callback_requests (call_id) WHERE call_id IS NOT NULL",
    "leads_call_idx": "leads (call_id) WHERE call_id IS NOT NULL",
    "notifications_call_idx": "notifications (call_id) WHERE call_id IS NOT NULL",
    "crm_syncs_call_idx": "crm_syncs (call_id) WHERE call_id IS NOT NULL",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, definition in INDEXES.items():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS knowledge_chunks_embedding_idx")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS knowledge_chunks_embedding_idx "
            "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
        )
        for name in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
