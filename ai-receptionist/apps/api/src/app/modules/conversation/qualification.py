"""Lead qualification: deterministic scoring against the tenant's configured
question flow.

No LLM call here — Claude (via the vendor's system prompt, see
tenants/service.py) conducts the qualification conversation and calls
`record_qualification_answer` per answer; this module only persists answers
and computes the running score. Concurrent answers within one call upsert
atomically via the (tenant_id, call_id) unique index (migration 0003) —
no read-then-write race.
"""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import tenant_session
from app.modules.conversation.models import Lead
from app.modules.tenants.models import AgentConfig


def score_answer(question_config: dict[str, Any], answer: Any) -> int:
    scoring = question_config.get("scoring") or {}
    if not scoring:
        return 0
    key = ("true" if answer else "false") if isinstance(answer, bool) else str(answer).lower()
    return int(scoring.get(key, 0))


async def record_answer(
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    caller_e164: str,
    question_key: str,
    answer: Any,
    agent_config: AgentConfig,
) -> tuple[uuid.UUID, int]:
    """Upsert the lead's qualification answers and running score for this
    call. Returns (lead_id, score_after_this_answer)."""
    question_config: dict[str, Any] = next(
        (q for q in agent_config.qualification if q.get("key") == question_key), {}
    )
    points = score_answer(question_config, answer)
    patch = {question_key: answer}

    async with tenant_session(tenant_id) as session:
        stmt = pg_insert(Lead).values(
            tenant_id=tenant_id,
            call_id=call_id,
            phone=caller_e164,
            qualification=patch,
            score=points,
        )
        upsert = stmt.on_conflict_do_update(
            # The target index is partial (WHERE call_id IS NOT NULL,
            # migration 0003), so ON CONFLICT must repeat that predicate via
            # index_where or Postgres won't match it.
            index_elements=["tenant_id", "call_id"],
            index_where=text("call_id IS NOT NULL"),
            set_={
                # Postgres upsert semantics: bare column = existing row,
                # excluded.<col> = the row we attempted to insert.
                "qualification": Lead.qualification.op("||")(stmt.excluded.qualification),
                "score": Lead.score + stmt.excluded.score,
            },
        ).returning(Lead.id, Lead.score)
        lead_id, score = (await session.execute(upsert)).one()

    return lead_id, score
