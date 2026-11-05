"""Tenant lookups.

`resolve_tenant_by_number` runs *before* tenant identity is known, so it uses
the unscoped session; a dedicated SELECT-only RLS policy (migration 0002)
permits number lookup when no tenant context is set. Everything else runs
tenant-scoped.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import admin_session, table_of, tenant_session
from app.core.errors import AuthenticationError, TenantNotFoundError
from app.modules.tenants.models import AgentConfig, Integration, PhoneNumber, Tenant, User


async def resolve_tenant_by_number(e164: str) -> tuple[Tenant, PhoneNumber]:
    """Map an inbound phone number to its tenant. The entry point of every call."""
    async with admin_session() as session:
        row = (
            await session.execute(
                select(Tenant, PhoneNumber)
                .join(PhoneNumber, PhoneNumber.tenant_id == Tenant.id)
                .where(PhoneNumber.e164 == e164, Tenant.status == "active")
            )
        ).first()
    if row is None:
        raise TenantNotFoundError(f"no active tenant for number {e164}")
    return row.Tenant, row.PhoneNumber


async def get_tenant(tenant_id: uuid.UUID) -> Tenant:
    """Load a tenant by id. Detached from the session so callers can read its
    config (business hours, timezone, settings) after the session closes."""
    async with admin_session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            raise TenantNotFoundError(f"no tenant {tenant_id}")
        session.expunge(tenant)
        return tenant


async def get_tenant_by_slug(slug: str) -> Tenant:
    """Resolve a tenant from its public slug — the dashboard login form's
    equivalent of `resolve_tenant_by_number`: identity has to come from
    *somewhere* before `app.tenant_id` can be set, and `tenants` itself
    carries no RLS (it's the root of tenant identity, not a tenant-scoped
    table), so this needs no special policy the way migration 0002 needed
    one for `phone_numbers`."""
    async with admin_session() as session:
        tenant = (
            await session.execute(
                select(Tenant).where(Tenant.slug == slug, Tenant.status == "active")
            )
        ).scalar_one_or_none()
        if tenant is None:
            raise TenantNotFoundError(f"no active tenant with slug {slug!r}")
        session.expunge(tenant)
        return tenant


async def get_user_by_email(tenant_id: uuid.UUID, email: str) -> User:
    """Tenant-scoped by design: two tenants may legitimately have a user
    sharing the same email (a consultant working with both practices), and
    `users.(tenant_id, email)` is unique per-tenant, not globally — the
    login form already carries the tenant slug, so there is no unscoped
    "find this email anywhere" lookup to build."""
    async with tenant_session(tenant_id) as session:
        user = (
            await session.execute(
                select(User).where(User.tenant_id == tenant_id, User.email == email)
            )
        ).scalar_one_or_none()
        if user is None:
            # Same exception as "wrong password" (see AuthenticationError) —
            # a login endpoint that raises differently for "no such user"
            # vs. "wrong password" is an email-enumeration oracle.
            raise AuthenticationError("invalid email or password")
        session.expunge(user)
        return user


async def get_active_agent_config(tenant_id: uuid.UUID) -> AgentConfig:
    async with tenant_session(tenant_id) as session:
        config = (
            await session.execute(
                select(AgentConfig).where(AgentConfig.tenant_id == tenant_id, AgentConfig.is_active)
            )
        ).scalar_one_or_none()
    if config is None:
        raise TenantNotFoundError(f"tenant {tenant_id} has no active agent config")
    return config


async def update_tenant(tenant_id: uuid.UUID, **fields: Any) -> Tenant:
    """Patch whichever of `timezone`/`business_hours`/`settings` were given.
    `settings` replaces the whole JSONB blob — the caller (routes.py) is
    responsible for merging over the existing value first, the same way a
    PATCH that only means to touch one key still has to send the rest."""
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(Tenant).update().where(Tenant.id == tenant_id).values(**fields)
        )
        tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        session.expunge(tenant)
        return tenant


async def list_integrations(tenant_id: uuid.UUID) -> list[Integration]:
    async with tenant_session(tenant_id) as session:
        rows = list(
            (
                await session.execute(select(Integration).where(Integration.tenant_id == tenant_id))
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


async def upsert_integration(
    tenant_id: uuid.UUID, provider: str, credentials_encrypted: str
) -> Integration:
    """Create-or-reconnect. `(tenant_id, provider)` is unique (migration
    0001), so a second "Connect" click after a revoke overwrites the old
    ciphertext and flips status back to 'connected' rather than colliding."""
    async with tenant_session(tenant_id) as session:
        stmt = (
            pg_insert(Integration)
            .values(
                tenant_id=tenant_id,
                provider=provider,
                credentials_encrypted=credentials_encrypted,
                status="connected",
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "provider"],
                set_={"credentials_encrypted": credentials_encrypted, "status": "connected"},
            )
            .returning(Integration.id)
        )
        integration_id = (await session.execute(stmt)).scalar_one()
        integration = (
            await session.execute(select(Integration).where(Integration.id == integration_id))
        ).scalar_one()
        session.expunge(integration)
        return integration


async def create_agent_config_version(tenant_id: uuid.UUID, **fields: Any) -> AgentConfig:
    """Write a dashboard edit as a *new* version rather than mutating the
    active row in place — agent_configs is versioned by design (README:
    "versioned agent configs"), and provision_voice.py always reads
    whichever row is_active, so flipping the flag here is what makes a saved
    edit actually reach the phone line on the next `make provision` run."""
    async with tenant_session(tenant_id) as session:
        current = (
            await session.execute(
                select(AgentConfig).where(AgentConfig.tenant_id == tenant_id, AgentConfig.is_active)
            )
        ).scalar_one()
        await session.execute(
            table_of(AgentConfig)
            .update()
            .where(AgentConfig.id == current.id)
            .values(is_active=False)
        )
        new_config = AgentConfig(
            tenant_id=tenant_id,
            version=current.version + 1,
            is_active=True,
            system_prompt=fields.get("system_prompt", current.system_prompt),
            first_message=fields.get("first_message", current.first_message),
            voice_id=fields.get("voice_id", current.voice_id),
            voice_provider=fields.get("voice_provider", current.voice_provider),
            voice_model=fields.get("voice_model", current.voice_model),
            language=fields.get("language", current.language),
            qualification=fields.get("qualification", current.qualification),
            escalation_policy=fields.get("escalation_policy", current.escalation_policy),
        )
        session.add(new_config)
        await session.flush()
        session.expunge(new_config)
        return new_config
