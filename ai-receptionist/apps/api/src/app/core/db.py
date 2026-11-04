"""Async database engine, sessions, and tenant-scoped RLS sessions.

Multi-tenant isolation is enforced in PostgreSQL via row-level security.
Every tenant-scoped query must run through `tenant_session()`, which sets the
`app.tenant_id` connection variable that the RLS policies check. The
application connects as a non-superuser role (`receptionist_app` in
production) so policies cannot be bypassed.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import sentry_sdk
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models. The schema itself is owned by
    Alembic migrations; models map to it and never create tables."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def table_of(model: Any) -> Table:
    """The Core table behind an ORM model, for plain `UPDATE`/`DELETE`
    statements that must not trigger ORM session synchronisation. Exists only
    to give `Model.__table__` (typed as a bare FromClause) its real type."""
    return cast(Table, model.__table__)


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Session scoped to one tenant; RLS policies filter on app.tenant_id.

    `SET LOCAL` binds the variable to the enclosing transaction, so the
    scope itself never outlives this block. But on a pooled connection that
    gets reused, Postgres's custom-GUC reset semantics can leave
    `current_setting('app.tenant_id', true)` returning '' rather than a
    true NULL on the *next* transaction over that same physical connection
    — the RLS policies guard against that (NULLIF(...,'') — see migration
    0001) and `admin_session()` clears it explicitly for the same reason.
    """
    factory = get_session_factory()
    sentry_sdk.set_tag("tenant_id", str(tenant_id))  # no-op when reporting is disabled
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        yield session


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Unscoped session for cross-tenant operations (tenant lookup, analytics
    rollups, migrations). Use sparingly and never with caller-supplied SQL.

    Explicitly clears app.tenant_id rather than relying on it being unset —
    see the pooled-connection caveat on `tenant_session()`.
    """
    factory = get_session_factory()
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        yield session


class UnsafeDatabaseRoleError(RuntimeError):
    """The process is connected as a role that bypasses row-level security."""


async def _role_flags() -> tuple[str, bool, bool]:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    return row[0], bool(row[1]), bool(row[2])


async def ensure_rls_enforced() -> None:
    """Refuse to run as a role that silently voids tenant isolation.

    Superusers and BYPASSRLS roles skip every row-level-security policy —
    FORCE ROW LEVEL SECURITY does not apply to them — while the policies still
    look correct in `\\d`. Managed Postgres hands out a superuser as the
    default login, so pointing DATABASE_URL at it "just works" and leaks
    across tenants without a single error. See infra/postgres/.

    Called at startup by the API and every worker in staging/production.
    """
    role, is_super, bypass_rls = await _role_flags()
    if is_super or bypass_rls:
        raise UnsafeDatabaseRoleError(
            f"database role {role!r} bypasses row-level security "
            f"(superuser={is_super}, bypassrls={bypass_rls}); tenant isolation would not "
            "apply. Connect as a NOSUPERUSER NOBYPASSRLS role — see "
            "infra/postgres/bootstrap-managed.sql."
        )


async def check_database() -> bool:
    async with get_session_factory()() as session:
        await session.execute(text("SELECT 1"))
    return True
