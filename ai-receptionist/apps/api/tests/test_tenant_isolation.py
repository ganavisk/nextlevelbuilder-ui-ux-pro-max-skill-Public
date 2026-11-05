"""Tenant isolation is enforced by Postgres RLS, not by application code.

That makes it worth testing the *mechanism* and not just its outcomes: the
policies can be flawless and still enforce nothing if the role the app
connects as is allowed to bypass them. A superuser (which is what the
official Postgres image makes POSTGRES_USER) bypasses RLS unconditionally —
`\\d` still prints the policies, every query still returns the "right" rows in
single-tenant testing, and the boundary is simply absent.

These tests fail loudly in that configuration.
"""

import uuid

from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.db import tenant_session
from app.modules.conversation.models import KnowledgeDoc
from tests.conftest import requires_services

RLS_TABLES = ("knowledge_docs", "knowledge_chunks", "appointments", "notifications", "calls")


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


@requires_services
def test_app_role_cannot_bypass_row_level_security() -> None:
    """The role in DATABASE_URL must be neither SUPERUSER nor BYPASSRLS.
    Either attribute silently disables every tenant_isolation policy."""
    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
        assert not row.rolsuper, (
            "the application's database role is a SUPERUSER, which bypasses RLS — "
            "tenant isolation is not enforced. Connect as a NOSUPERUSER role."
        )
        assert not row.rolbypassrls, (
            "the application's database role has BYPASSRLS — tenant isolation is not enforced."
        )
    finally:
        engine.dispose()


@requires_services
def test_rls_is_enabled_and_forced_on_tenant_tables() -> None:
    """FORCE matters separately from ENABLE: without it the table owner — which
    is the app itself, since it runs the migrations — is exempt from its own
    policies."""
    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": list(RLS_TABLES)},
            ).all()
        found = {r.relname: (r.relrowsecurity, r.relforcerowsecurity) for r in rows}
        assert set(found) == set(RLS_TABLES), f"missing tables: {set(RLS_TABLES) - set(found)}"
        for name, (enabled, forced) in found.items():
            assert enabled, f"{name} does not have row level security enabled"
            assert forced, f"{name} does not FORCE row level security"
    finally:
        engine.dispose()


@requires_services
def test_every_tenant_table_is_protected_found_from_the_catalog() -> None:
    """The list above was written by hand when there were five tenant tables;
    there are fifteen now. This asks Postgres instead: every table with a
    tenant_id must enable and FORCE row-level security, and carry a
    tenant_isolation policy that checks both reads (USING) and writes
    (WITH CHECK). A table added without them fails here, even if no test
    ever writes to it."""
    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "p.qual IS NOT NULL AS has_using, p.with_check IS NOT NULL AS has_check "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
                    "JOIN information_schema.columns col "
                    "  ON col.table_schema = 'public' AND col.table_name = c.relname "
                    "  AND col.column_name = 'tenant_id' "
                    "LEFT JOIN pg_policies p ON p.schemaname = 'public' "
                    "  AND p.tablename = c.relname AND p.policyname = 'tenant_isolation' "
                    "WHERE c.relkind = 'r'"
                )
            ).all()
        assert len(rows) >= len(RLS_TABLES)
        unprotected = [
            r.relname
            for r in rows
            if not (r.relrowsecurity and r.relforcerowsecurity and r.has_using and r.has_check)
        ]
        assert unprotected == [], f"tenant tables without full RLS: {unprotected}"
    finally:
        engine.dispose()


@requires_services
async def test_one_tenant_cannot_read_anothers_rows() -> None:
    """End-to-end proof through the real session helper: a query with no
    tenant_id predicate of its own still returns only the caller's rows."""
    engine = _sync_engine()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        for tid, slug in ((tenant_a, "iso-a"), (tenant_b, "iso-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, vertical) "
                    "VALUES (:id, :slug, 'Isolation Test', 'dental')"
                ),
                {"id": str(tid), "slug": f"{slug}-{tid.hex[:6]}"},
            )
    try:
        async with tenant_session(tenant_b) as session:
            session.add(
                KnowledgeDoc(tenant_id=tenant_b, title="B only", content="tenant b private text")
            )

        async with tenant_session(tenant_a) as session:
            # Deliberately unfiltered: RLS is the only thing scoping this.
            rows = (await session.execute(text("SELECT title FROM knowledge_docs"))).all()
        assert rows == [], f"tenant A read tenant B's rows: {rows}"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                {"a": str(tenant_a), "b": str(tenant_b)},
            )
        engine.dispose()


@requires_services
async def test_tenant_cannot_insert_rows_owned_by_another_tenant() -> None:
    """The WITH CHECK half of the policy: forging a foreign tenant_id on write
    must be rejected, not merely invisible afterwards."""
    import sqlalchemy.exc

    engine = _sync_engine()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        for tid, slug in ((tenant_a, "iso-w-a"), (tenant_b, "iso-w-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, vertical) "
                    "VALUES (:id, :slug, 'Isolation Test', 'dental')"
                ),
                {"id": str(tid), "slug": f"{slug}-{tid.hex[:6]}"},
            )
    try:
        raised = False
        try:
            async with tenant_session(tenant_a) as session:
                session.add(
                    KnowledgeDoc(tenant_id=tenant_b, title="forged", content="written as A")
                )
                await session.flush()
        except sqlalchemy.exc.ProgrammingError as exc:
            raised = "row-level security" in str(exc).lower()
        assert raised, "a tenant was able to insert a row owned by another tenant"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                {"a": str(tenant_a), "b": str(tenant_b)},
            )
        engine.dispose()


@requires_services
async def test_app_sessions_always_plan_with_actual_values() -> None:
    """Generic plans undo the time-window indexes (PHASE-8 doc, section 3),
    so every app connection runs with force_custom_plan."""
    async with tenant_session(uuid.uuid4()) as session:
        mode = (await session.execute(text("SHOW plan_cache_mode"))).scalar_one()
    assert mode == "force_custom_plan"
