"""Integration test: the qualification upsert against real Postgres —
proves the ON CONFLICT DO UPDATE merges JSONB answers and accumulates score
correctly, including under concurrent writes for the same call (migration
0003's unique index is what makes this atomic instead of racy)."""

import asyncio
import uuid

from sqlalchemy import create_engine, select, text

from app.core.config import Settings
from app.core.db import tenant_session
from app.modules.conversation.models import Lead
from app.modules.conversation.qualification import record_answer
from app.modules.tenants.models import AgentConfig
from tests.conftest import requires_services


def _seed_tenant_and_call(conn, tenant_id, call_id, slug_prefix) -> None:
    """Seed a tenant and a call row. record_answer writes a lead whose
    call_id FKs to calls, so the call must exist first — which it always
    does in a real flow (call_started precedes any qualification tool call)."""
    conn.execute(
        text(
            "INSERT INTO tenants (id, slug, name, vertical) "
            "VALUES (:id, :slug, 'Qual Test', 'dental')"
        ),
        {"id": str(tenant_id), "slug": f"{slug_prefix}-{tenant_id.hex[:6]}"},
    )
    conn.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
    conn.execute(
        text(
            "INSERT INTO calls (id, tenant_id, vendor_call_id, caller_e164) "
            "VALUES (:cid, :tid, :vcid, '+15550001111')"
        ),
        {"cid": str(call_id), "tid": str(tenant_id), "vcid": f"qual-{call_id.hex[:8]}"},
    )


def _agent_config(tenant_id: uuid.UUID) -> AgentConfig:
    return AgentConfig(
        tenant_id=tenant_id,
        version=1,
        is_active=True,
        system_prompt="x",
        first_message="x",
        qualification=[
            {"key": "pain", "question": "In pain?", "type": "boolean", "scoring": {"true": 30}},
            {"key": "insurance", "question": "Insurance?", "type": "text"},
        ],
    )


@requires_services
async def test_sequential_answers_merge_and_accumulate_score() -> None:
    settings = Settings(_env_file=None, app_env="test")
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)
    tenant_id = uuid.uuid4()
    call_id = uuid.uuid4()

    with engine.begin() as conn:
        _seed_tenant_and_call(conn, tenant_id, call_id, "qual-test")
    config = _agent_config(tenant_id)

    try:
        lead_id_1, score_1 = await record_answer(
            tenant_id, call_id, "+15550001111", "pain", True, config
        )
        lead_id_2, score_2 = await record_answer(
            tenant_id, call_id, "+15550001111", "insurance", "Delta Dental", config
        )

        assert lead_id_1 == lead_id_2  # same call -> same lead row, not a duplicate
        assert score_1 == 30
        assert score_2 == 30  # "insurance" has no scoring config -> +0

        async with tenant_session(tenant_id) as session:
            lead = (await session.execute(select(Lead).where(Lead.id == lead_id_1))).scalar_one()
            assert lead.qualification == {"pain": True, "insurance": "Delta Dental"}
            assert lead.score == 30
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
async def test_concurrent_answers_for_same_call_do_not_duplicate_lead() -> None:
    settings = Settings(_env_file=None, app_env="test")
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)
    tenant_id = uuid.uuid4()
    call_id = uuid.uuid4()

    with engine.begin() as conn:
        _seed_tenant_and_call(conn, tenant_id, call_id, "qual-race")
    config = _agent_config(tenant_id)

    try:
        results = await asyncio.gather(
            record_answer(tenant_id, call_id, "+15550001111", "pain", True, config),
            record_answer(tenant_id, call_id, "+15550001111", "insurance", "Cigna", config),
        )
        lead_ids = {lead_id for lead_id, _ in results}
        assert len(lead_ids) == 1  # the unique index forced convergence on one row

        async with tenant_session(tenant_id) as session:
            count = (
                await session.execute(
                    select(Lead.id).where(Lead.tenant_id == tenant_id, Lead.call_id == call_id)
                )
            ).all()
            assert len(count) == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()
