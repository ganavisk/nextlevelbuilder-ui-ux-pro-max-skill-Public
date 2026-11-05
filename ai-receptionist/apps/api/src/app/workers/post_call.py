"""Post-call worker: classifies outcome/sentiment and writes a one-line
summary via the fast model. Runs off `queue:post_call`; every step is
idempotent so a crash mid-batch is safe to retry.

CRM sync is *enqueued* from here rather than performed here, so that a
tenant's CRM being down can never delay call classification. It is enqueued
after classification so the CRM receives the summary and outcome rather than
a bare phone number.

Run: python -m app.workers.post_call
"""

import asyncio
import functools
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update

import app.models  # noqa: F401 — registers every ORM model on Base.metadata
from app.core.config import get_settings
from app.core.db import ensure_rls_enforced, tenant_session
from app.core.logging import configure_logging, get_logger
from app.core.observability import init_sentry
from app.core.queue import (
    CRM_QUEUE,
    POST_CALL_QUEUE,
    enqueue,
    reclaim_orphans,
    run_worker,
    stop_on_signal,
    worker_redis,
)
from app.modules.conversation.pricing import estimate_cost_cents
from app.modules.conversation.providers import get_llm_provider
from app.modules.conversation.providers.base import LLMProvider
from app.modules.conversation.summarization import summarize_call
from app.modules.telephony.models import Call, Transcript

logger = get_logger(__name__)


async def _enqueue_crm(
    redis: Redis,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    outcome: str | None,
    summary: str | None,
    duration_seconds: int,
) -> None:
    await enqueue(
        redis,
        CRM_QUEUE,
        {
            "type": "crm_sync",
            "tenant_id": str(tenant_id),
            "call_id": str(call_id),
            "outcome": outcome,
            "summary": summary,
            "duration_seconds": duration_seconds,
        },
    )


async def handle_post_call(
    job: dict[str, Any], *, llm: LLMProvider | None = None, redis: Redis | None = None
) -> None:
    llm = llm or get_llm_provider(get_settings())
    tenant_id = uuid.UUID(job["tenant_id"])
    call_id = uuid.UUID(job["call_id"])

    async with tenant_session(tenant_id) as session:
        call = await session.get(Call, call_id)
        if call is None:
            return  # the call vanished — nothing to do
        duration_seconds = (
            int((call.ended_at - call.started_at).total_seconds())
            if call.ended_at and call.started_at
            else 0
        )
        simulated = call.direction == "simulated"  # test lab: never reaches the CRM
        if call.outcome is not None:
            # Classified on an earlier attempt. That attempt may have died
            # after committing the classification but before the CRM job was
            # enqueued, and this retry is the only thing that can notice —
            # so hand off again rather than returning. A duplicate CRM job is
            # harmless: the crm_syncs ledger turns it into an "already
            # synced" no-op.
            if redis is not None and not simulated:
                await _enqueue_crm(
                    redis, tenant_id, call_id, call.outcome, call.summary or "", duration_seconds
                )
            return
        turns = (
            await session.execute(select(Transcript.turns).where(Transcript.call_id == call_id))
        ).scalar_one_or_none() or []

    result = await summarize_call(turns, llm)
    cost_cents = estimate_cost_cents(result.model, result.input_tokens, result.output_tokens)

    async with tenant_session(tenant_id) as session:
        # WHERE outcome IS NULL keeps this a no-op if another worker already
        # finalized the call between the read above and this write — the
        # only cost of that race is a redundant LLM call, never a bad write.
        await session.execute(
            update(Call)
            .where(Call.id == call_id, Call.outcome.is_(None))
            .values(llm_cost_cents=Call.llm_cost_cents + cost_cents, **result.classification)
        )
    logger.info(
        "post_call_processed",
        tenant_id=str(tenant_id),
        call_id=str(call_id),
        **result.classification,
    )

    # Hand off to the CRM worker. Enqueued, not called: a tenant's CRM being
    # down must never delay or fail call classification, which has already
    # been committed above.
    if redis is not None and not simulated:
        await _enqueue_crm(
            redis,
            tenant_id,
            call_id,
            result.classification.get("outcome"),
            result.classification.get("summary", ""),
            duration_seconds,
        )


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_sentry(settings, service="worker-post-call")
    if settings.app_env in ("staging", "production"):
        await ensure_rls_enforced()
    redis = worker_redis(settings.redis_url)
    stop = stop_on_signal()
    llm = get_llm_provider(settings)  # constructed once, reused across jobs
    await reclaim_orphans(redis, POST_CALL_QUEUE)
    logger.info("post_call_worker_started", queue=POST_CALL_QUEUE)
    try:
        await run_worker(
            redis,
            POST_CALL_QUEUE,
            functools.partial(handle_post_call, llm=llm, redis=redis),
            stop=stop,
        )
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
