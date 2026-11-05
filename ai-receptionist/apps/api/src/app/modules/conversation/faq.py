"""FAQ answering: RAG-grounded question answering.

Never answers outside the retrieved context — this is the primary
hallucination guardrail from Phase 1 Risk R3. No relevant chunks, an empty
model response (including a safety refusal, which yields no text blocks),
or a completion failure all route to the same safe fallback: tell the caller
we'll follow up rather than risk a wrong answer.
"""

import uuid

from sqlalchemy import update

from app.core.db import tenant_session
from app.core.logging import get_logger
from app.modules.conversation.pricing import estimate_cost_cents
from app.modules.conversation.providers.base import (
    CompletionRequest,
    EmbeddingProvider,
    LLMProvider,
)
from app.modules.conversation.retrieval import retrieve_relevant_chunks
from app.modules.telephony.models import Call

logger = get_logger(__name__)

NO_ANSWER_FALLBACK = (
    "I don't have that information on hand, but I'll make a note and have "
    "someone from the team follow up with you."
)

_SYSTEM_TEMPLATE = """You are answering a caller's question using ONLY the context below. \
If the context does not contain the answer, respond with exactly: "{fallback}" \
Do not use outside knowledge or make anything up. Keep the answer to 2-3 sentences, in a \
natural spoken-language style — no markdown, no lists, no headers.

Context:
{context}"""


async def answer_faq(
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    question: str,
    llm: LLMProvider,
    embeddings: EmbeddingProvider,
) -> str:
    chunks = await retrieve_relevant_chunks(tenant_id, question, embeddings)
    if not chunks:
        logger.info("faq_no_relevant_chunks", tenant_id=str(tenant_id))
        return NO_ANSWER_FALLBACK

    context = "\n\n".join(c.content for c in chunks)
    system = _SYSTEM_TEMPLATE.format(fallback=NO_ANSWER_FALLBACK, context=context)

    try:
        result = await llm.complete(
            CompletionRequest(
                system=system,
                messages=[{"role": "user", "content": question}],
                max_tokens=300,
            )
        )
    except Exception:
        logger.exception("faq_completion_failed", tenant_id=str(tenant_id))
        return NO_ANSWER_FALLBACK

    await _record_cost(tenant_id, call_id, result.model, result.input_tokens, result.output_tokens)
    return result.text.strip() or NO_ANSWER_FALLBACK


async def _record_cost(
    tenant_id: uuid.UUID, call_id: uuid.UUID, model: str, input_tokens: int, output_tokens: int
) -> None:
    """Best-effort cost metering — a DB hiccup here must never break the
    in-call tool response the caller is waiting on."""
    cost_cents = estimate_cost_cents(model, input_tokens, output_tokens)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(Call)
                .where(Call.id == call_id)
                .values(llm_cost_cents=Call.llm_cost_cents + cost_cents)
            )
    except Exception:
        logger.exception("cost_metering_update_failed", tenant_id=str(tenant_id))
