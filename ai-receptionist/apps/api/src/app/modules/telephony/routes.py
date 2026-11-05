"""Voice webhook ingress. Verify → normalize → dispatch.

TOOL_CALL events are the one exception to "nothing else": Vapi expects the
tool result in the same HTTP response, so this route resolves the in-flight
call, hands each requested tool to the conversation module's executor, and
formats the results back through the adapter — still within the hot-path
budget (no integration I/O happens in the tools that can run here; see
conversation/tools.py).
"""

import json
from typing import Any

from fastapi import APIRouter, Request

from app.core.config import Settings
from app.core.errors import ValidationFailedError
from app.modules.conversation.tools import ToolExecutor
from app.modules.telephony.providers import get_voice_provider
from app.modules.telephony.providers.base import CallEventType
from app.modules.telephony.service import TelephonyService
from app.modules.tenants.repository import get_active_agent_config, get_tenant

router = APIRouter()


@router.post("/vapi")
async def vapi_webhook(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    provider = get_voice_provider(settings)

    body = await request.body()
    provider.verify_webhook(body, dict(request.headers))

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationFailedError("webhook body is not valid JSON") from exc

    event = provider.parse_event(payload)
    service = TelephonyService(request.app.state.redis, settings)

    if event.event_type is CallEventType.TOOL_CALL:
        tenant_id, call_id = await service.resolve_call_context(event)
        tenant = await get_tenant(tenant_id)
        agent_config = await get_active_agent_config(tenant_id)
        executor = ToolExecutor(
            request.app.state.llm_provider,
            request.app.state.embedding_provider,
            calendar=request.app.state.calendar_provider,
            redis=request.app.state.redis,
            read_deadline_seconds=settings.tool_deadline_seconds,
        )

        results = []
        for tool_call in provider.extract_tool_calls(event):
            text = await executor.dispatch(
                tenant=tenant,
                call_id=call_id,
                caller_e164=event.from_number,
                agent_config=agent_config,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
            )
            results.append((tool_call.id, text))

        await service.handle_event(event)  # audit trail (call_events row)
        return provider.format_tool_results(results)

    return await service.handle_event(event)
