"""Dashboard API: login, and reading/editing the signed-in tenant's own
config. Every route but login requires a bearer token (`core.auth`); every
route scopes to `auth.tenant_id` from that token — there is no endpoint that
takes a tenant id as a path/query parameter, so there is no way to ask for
another tenant's data by guessing an id.
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request

from app.core.auth import AuthContext, get_current_user, require_role
from app.core.config import Settings
from app.core.errors import ValidationFailedError
from app.core.util import deep_merge
from app.modules.escalation.service import example_next_open, validate_promises
from app.modules.tenants.login import login as login_user
from app.modules.tenants.repository import (
    create_agent_config_version,
    get_active_agent_config,
    get_tenant,
    update_tenant,
)
from app.modules.tenants.schemas import (
    AgentConfigSummary,
    AgentConfigUpdateRequest,
    CallbackPromisesPreview,
    CallbackPromisesUpdate,
    LoginRequest,
    LoginResponse,
    TenantSummary,
    TenantUpdateRequest,
    UserSummary,
    VoiceSyncSummary,
)
from app.modules.tenants.voice_sync import (
    agent_snapshot,
    get_status,
    request_sync,
    resync_if_changed,
)

router = APIRouter()


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request) -> LoginResponse:
    settings: Settings = request.app.state.settings
    result = await login_user(
        tenant_slug=body.tenant_slug,
        email=body.email,
        password=body.password,
        settings=settings,
        redis=request.app.state.redis,
        # Behind the platform's proxy this is the forwarded client address
        # (uvicorn --proxy-headers; see docker-entrypoint.sh).
        client_ip=request.client.host if request.client else "unknown",
    )
    return LoginResponse(
        access_token=result.access_token,
        tenant=TenantSummary.model_validate(result.tenant),
        user=UserSummary.model_validate(result.user),
    )


@router.get("/tenants/me", response_model=TenantSummary)
async def get_my_tenant(auth: AuthContext = Depends(get_current_user)) -> TenantSummary:
    tenant = await get_tenant(auth.tenant_id)
    return TenantSummary.model_validate(tenant)


def _valid_timezone(name: str) -> str:
    """Every booking time and the voice agent's clock are computed in this
    zone; an unknown name would break both, so refuse it at the door."""
    name = name.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationFailedError(
            f"{name!r} isn't a timezone name. Use an IANA name such as America/Chicago."
        ) from None
    return name


@router.patch("/tenants/me", response_model=TenantSummary)
async def update_my_tenant(
    body: TenantUpdateRequest, request: Request, auth: AuthContext = Depends(require_role("admin"))
) -> TenantSummary:
    fields: dict[str, Any] = {}
    if body.timezone is not None:
        fields["timezone"] = _valid_timezone(body.timezone)
    if body.business_hours is not None:
        fields["business_hours"] = body.business_hours
    if body.settings is not None:
        # Deep-merge over the current value (see TenantUpdateRequest's
        # docstring and core.util.deep_merge) rather than replacing it
        # outright — settings nests at least three levels deep in practice
        # (settings.crm.stage_by_outcome.<outcome>), so a shallow merge would
        # silently delete siblings of whatever key the caller named.
        current = await get_tenant(auth.tenant_id)
        fields["settings"] = deep_merge(current.settings, body.settings)
    if not fields:
        return TenantSummary.model_validate(await get_tenant(auth.tenant_id))
    before = await agent_snapshot(auth.tenant_id)
    tenant = await update_tenant(auth.tenant_id, **fields)
    # Timezone, booking fields, services and forbidden topics all shape the
    # phone agent; business hours and CRM settings don't. The diff decides.
    await resync_if_changed(auth.tenant_id, before, redis=request.app.state.redis)
    return TenantSummary.model_validate(tenant)


@router.get("/tenants/me/agent-config", response_model=AgentConfigSummary)
async def get_my_agent_config(
    auth: AuthContext = Depends(get_current_user),
) -> AgentConfigSummary:
    config = await get_active_agent_config(auth.tenant_id)
    return AgentConfigSummary.model_validate(config)


@router.patch("/tenants/me/agent-config", response_model=AgentConfigSummary)
async def update_my_agent_config(
    body: AgentConfigUpdateRequest,
    request: Request,
    auth: AuthContext = Depends(require_role("admin")),
) -> AgentConfigSummary:
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    policy = fields.get("escalation_policy")
    if policy is not None and ("callback_promise" in policy or "after_hours_promise" in policy):
        # Same checks as the Settings editor (PUT /tenants/me/callback-promises),
        # so the raw API can't save a promise callers would hear garbled.
        validate_promises(
            policy.get("callback_promise", "Someone from the team will call you back."),
            policy.get("after_hours_promise", ""),
        )
    before = await agent_snapshot(auth.tenant_id)
    config = await create_agent_config_version(auth.tenant_id, **fields)
    await resync_if_changed(auth.tenant_id, before, redis=request.app.state.redis)
    return AgentConfigSummary.model_validate(config)


@router.put("/tenants/me/callback-promises", response_model=AgentConfigSummary)
async def update_my_callback_promises(
    body: CallbackPromisesUpdate, auth: AuthContext = Depends(require_role("admin"))
) -> AgentConfigSummary:
    """Edit what callers are told after asking for a call back. Saved as a
    new agent-config version like any other edit. The callback tool reads the
    active version on every call, so there is nothing to push to the voice
    vendor: the next call uses it."""
    texts = validate_promises(body.callback_promise, body.after_hours_promise)
    current = await get_active_agent_config(auth.tenant_id)
    policy = {**(current.escalation_policy or {}), **texts}
    if not texts["after_hours_promise"]:
        policy.pop("after_hours_promise")
    config = await create_agent_config_version(auth.tenant_id, escalation_policy=policy)
    return AgentConfigSummary.model_validate(config)


@router.get("/tenants/me/callback-promises/preview", response_model=CallbackPromisesPreview)
async def preview_my_callback_promises(
    auth: AuthContext = Depends(get_current_user),
) -> CallbackPromisesPreview:
    tenant = await get_tenant(auth.tenant_id)
    example = example_next_open(tenant.business_hours or {}, tenant.timezone, datetime.now(UTC))
    return CallbackPromisesPreview(next_open_example=example)


@router.get("/tenants/me/voice-sync", response_model=VoiceSyncSummary)
async def get_my_voice_sync(auth: AuthContext = Depends(get_current_user)) -> VoiceSyncSummary:
    status = await get_status(auth.tenant_id)
    return VoiceSyncSummary(state=status.state, synced_at=status.synced_at, error=status.error)


@router.post("/tenants/me/voice-sync", response_model=VoiceSyncSummary)
async def retry_my_voice_sync(
    request: Request, auth: AuthContext = Depends(require_role("admin"))
) -> VoiceSyncSummary:
    """Push the current settings to the phone agent again (after a failure)."""
    await request_sync(auth.tenant_id, redis=request.app.state.redis)
    status = await get_status(auth.tenant_id)
    return VoiceSyncSummary(state=status.state, synced_at=status.synced_at, error=status.error)
