"""Post-call summarization: classifies outcome and sentiment and writes a
one-line summary. Runs on the async post-call path only, using the fast
model (Haiku) — a classification/summarization task, per Phase 1 §5.1.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, TypedDict

from app.core.logging import get_logger
from app.modules.conversation.providers.base import CompletionRequest, LLMProvider

logger = get_logger(__name__)

VALID_OUTCOMES = {
    "answered_faq",
    "lead_qualified",
    "appointment_booked",
    "callback_requested",
    "voicemail",
    "abandoned",
    "other",
}
VALID_SENTIMENTS = {"positive", "neutral", "negative"}

_SYSTEM = """Classify this phone call transcript. Respond with ONLY a JSON object, \
no other text, matching exactly this shape:
{"summary": "<one sentence, under 20 words>", "outcome": "<one of: answered_faq, \
lead_qualified, appointment_booked, callback_requested, voicemail, abandoned, other>", \
"sentiment": "<one of: positive, neutral, negative>"}"""


class CallClassification(TypedDict):
    summary: str | None
    outcome: str
    sentiment: str | None


@dataclass(frozen=True)
class SummarizationResult:
    classification: CallClassification
    input_tokens: int
    output_tokens: int
    model: str


_FALLBACK: CallClassification = {"summary": None, "outcome": "other", "sentiment": None}


def _format_transcript(turns: list[dict[str, Any]]) -> str:
    return "\n".join(f"{turn.get('role', '?')}: {turn.get('text', '')}" for turn in turns)


async def summarize_call(turns: list[dict[str, Any]], llm: LLMProvider) -> SummarizationResult:
    """Never raises — any parse or completion failure degrades to the safe
    fallback (outcome="other", zero token usage) so the worker can never
    crash on bad output."""
    if not turns:
        return SummarizationResult(_FALLBACK.copy(), 0, 0, "none")

    try:
        result = await llm.complete(
            CompletionRequest(
                system=_SYSTEM,
                messages=[{"role": "user", "content": _format_transcript(turns)}],
                max_tokens=200,
            ),
            fast=True,
        )
        match = re.search(r"\{.*\}", result.text, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
    except Exception:
        logger.exception("call_summarization_failed")
        return SummarizationResult(_FALLBACK.copy(), 0, 0, "none")

    summary = data.get("summary")
    outcome = data.get("outcome")
    sentiment = data.get("sentiment")
    classification: CallClassification = {
        "summary": summary if isinstance(summary, str) else None,
        "outcome": outcome if outcome in VALID_OUTCOMES else "other",
        "sentiment": sentiment if sentiment in VALID_SENTIMENTS else None,
    }
    return SummarizationResult(
        classification, result.input_tokens, result.output_tokens, result.model
    )
