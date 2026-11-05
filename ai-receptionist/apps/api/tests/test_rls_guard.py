"""Startup refuses a database role that bypasses row-level security.

Managed Postgres hands out a superuser as its default login. Connected as one,
every RLS policy is silently skipped — queries still succeed, nothing errors,
and tenants can read each other's calls — so the processes must refuse to start
rather than run in that state.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.core.db as db
from app.core.config import Settings
from app.core.db import UnsafeDatabaseRoleError, ensure_rls_enforced
from app.main import create_app
from app.workers import confirmations, crm_sync, post_call
from tests.conftest import requires_services

STRONG_KEY = "k" * 40


def _flags(role: str, *, superuser: bool, bypassrls: bool):
    async def fake() -> tuple[str, bool, bool]:
        return role, superuser, bypassrls

    return patch.object(db, "_role_flags", fake)


@requires_services
async def test_the_application_role_passes() -> None:
    """The role the test suite (and the compose stack) connects as is the
    restricted one — this is the real database, not a stub."""
    await ensure_rls_enforced()


@pytest.mark.parametrize(("superuser", "bypassrls"), [(True, False), (False, True), (True, True)])
async def test_a_role_that_bypasses_rls_is_rejected(superuser: bool, bypassrls: bool) -> None:
    with (
        _flags("postgres", superuser=superuser, bypassrls=bypassrls),
        pytest.raises(UnsafeDatabaseRoleError, match="postgres"),
    ):
        await ensure_rls_enforced()


async def test_the_error_says_how_to_fix_it() -> None:
    with (
        _flags("postgres", superuser=True, bypassrls=False),
        pytest.raises(UnsafeDatabaseRoleError, match="bootstrap-managed.sql"),
    ):
        await ensure_rls_enforced()


def test_the_api_refuses_to_boot_in_production_on_an_unsafe_role() -> None:
    app = create_app(Settings(_env_file=None, app_env="production", secret_key=STRONG_KEY))
    with (
        _flags("postgres", superuser=True, bypassrls=False),
        pytest.raises(UnsafeDatabaseRoleError),
        TestClient(app),
    ):
        pass


@requires_services
def test_the_api_boots_in_production_on_a_restricted_role() -> None:
    app = create_app(Settings(_env_file=None, app_env="production", secret_key=STRONG_KEY))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_development_does_not_require_a_database_to_start() -> None:
    """Unit tests and `make dev` boot without Postgres; the guard is for
    deployed environments."""
    app = create_app(Settings(_env_file=None, app_env="development"))
    with _flags("postgres", superuser=True, bypassrls=False), TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("worker", [post_call, confirmations, crm_sync])
async def test_every_worker_refuses_to_start_in_production_on_an_unsafe_role(worker) -> None:
    settings = Settings(_env_file=None, app_env="staging", secret_key=STRONG_KEY)
    with (
        patch.object(worker, "get_settings", lambda: settings),
        _flags("postgres", superuser=True, bypassrls=False),
        pytest.raises(UnsafeDatabaseRoleError),
    ):
        await worker.main()
