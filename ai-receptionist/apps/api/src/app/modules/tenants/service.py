"""Tenant-facing assembly: turn stored configuration into what the voice
vendor needs. Prompt assembly stays here (platform-owned), never in the
vendor adapter."""

import uuid
from typing import Any

from app.core.config import get_settings
from app.modules.conversation.tools import booking_fields, tool_schemas_for
from app.modules.telephony.providers.base import AgentDefinition
from app.modules.tenants.models import AgentConfig, Tenant
from app.modules.tenants.repository import get_active_agent_config


def _format_qualification_flow(questions: list[dict[str, Any]]) -> str:
    if not questions:
        return ""
    lines = []
    for q in questions:
        line = f'- key="{q["key"]}": "{q["question"]}"'
        # Scoring matches the recorded answer exactly, so for a choice the
        # model must record one of the configured values, not a paraphrase
        # ("needs_tow", not "it needs a tow"), or the answer scores nothing.
        if q.get("options"):
            line += " (record exactly one of: " + ", ".join(str(o) for o in q["options"]) + ")"
        elif q.get("type") == "boolean":
            line += " (record true or false)"
        lines.append(line)
    return (
        "\n\nAsk these qualification questions naturally over the course of the call "
        "(not as a rigid checklist) and call record_qualification_answer with the "
        "matching key immediately after each answer:\n" + "\n".join(lines)
    )


def assemble_system_prompt(tenant: Tenant, config: AgentConfig) -> str:
    """Compose the runtime system prompt from tenant config. Guardrails
    (forbidden topics, escalation promise) are part of the prompt contract,
    not optional decoration — they implement the Risk R3 mitigation."""
    forbidden = tenant.settings.get("forbidden_topics", [])
    forbidden_block = (
        "\n\nYou must never discuss: " + "; ".join(forbidden) + ". "
        "If asked, use the request_callback tool instead."
        if forbidden
        else ""
    )
    qualification_block = _format_qualification_flow(config.qualification)
    fields = booking_fields(tenant)
    booking_block = (
        "\n\nBefore calling book_appointment, also ask for: "
        + "; ".join(f["description"] for f in fields)
        + ". Pass each answer in its matching book_appointment field."
        if fields
        else ""
    )
    escalation = config.escalation_policy.get(
        "callback_promise", "Someone from the team will call you back."
    )
    return (
        f"{config.system_prompt}{forbidden_block}{qualification_block}{booking_block}\n\n"
        f"For any factual question about the business (hours, services, prices, "
        f"policies, location) use the answer_faq tool rather than answering from memory.\n\n"
        f"If the caller asks for a human or you cannot help, use the request_callback "
        f'tool: collect their name, number, and reason first, then say: "{escalation}"'
    )


async def build_agent_definition(tenant: Tenant, tenant_id: uuid.UUID) -> AgentDefinition:
    config = await get_active_agent_config(tenant_id)
    settings = get_settings()
    return AgentDefinition(
        tenant_id=tenant_id,
        system_prompt=assemble_system_prompt(tenant, config),
        first_message=config.first_message,
        voice_id=config.voice_id,
        voice_provider=config.voice_provider,
        voice_model=config.voice_model,
        language=config.language,
        tools=tool_schemas_for(tenant),
        max_duration_seconds=settings.max_call_duration_seconds,
    )
