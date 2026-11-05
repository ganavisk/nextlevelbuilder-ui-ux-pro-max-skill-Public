"""Login, tenant config, and agent-config dashboard routes — driven through
the real HTTP routes against live Postgres, the same way test_call_flow.py
drives the voice webhook."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app
from tests.conftest import requires_services


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


def _seed_tenant_with_users(engine, *, slug: str) -> uuid.UUID:
    """A tenant with an owner (adminpass) and a viewer (viewpass), one
    active agent config, and a nested settings blob (to exercise deep-merge
    on PATCH)."""
    tenant_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, timezone, business_hours, "
                "settings) VALUES (:id, :slug, 'Auth Route Test', 'dental', "
                "'America/New_York', '{}', CAST(:settings AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": slug,
                "settings": '{"crm": {"enabled": true, "deal_min_score": 30, '
                '"stage_by_outcome": {"default": "new"}}}',
            },
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO agent_configs (tenant_id, version, is_active, system_prompt, "
                "first_message, qualification, escalation_policy) VALUES "
                "(:tid, 1, true, 'You are a receptionist.', 'Hello!', "
                '\'[{"key": "pain", "question": "In pain?"}]\', '
                '\'{"callback_promise": "We will call."}\')'
            ),
            {"tid": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO users (tenant_id, email, display_name, role, password_hash) "
                "VALUES (:tid, 'owner@test.example', 'Owner', 'owner', :hash)"
            ),
            {"tid": str(tenant_id), "hash": hash_password("adminpass")},
        )
        conn.execute(
            text(
                "INSERT INTO users (tenant_id, email, display_name, role, password_hash) "
                "VALUES (:tid, 'viewer@test.example', 'Viewer', 'viewer', :hash)"
            ),
            {"tid": str(tenant_id), "hash": hash_password("viewpass")},
        )
    return tenant_id


def _login(client: TestClient, *, slug: str, email: str, password: str) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.json()
    return response.json()


# ── login ────────────────────────────────────────────────────────────────


@requires_services
def test_login_succeeds_with_correct_credentials() -> None:
    engine = _sync_engine()
    slug = f"auth-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            body = _login(client, slug=slug, email="owner@test.example", password="adminpass")
            assert body["user"]["role"] == "owner"
            assert body["tenant"]["slug"] == slug
            assert body["token_type"] == "bearer"
            assert body["access_token"]
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_login_fails_with_wrong_password() -> None:
    engine = _sync_engine()
    slug = f"auth-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            r = client.post(
                "/api/v1/auth/login",
                json={"tenant_slug": slug, "email": "owner@test.example", "password": "wrong"},
            )
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "authentication_required"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_login_fails_for_unknown_email_with_the_same_error_as_wrong_password() -> None:
    """Email-enumeration guard: the two failure modes must be
    indistinguishable to the caller."""
    engine = _sync_engine()
    slug = f"auth-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            wrong_password = client.post(
                "/api/v1/auth/login",
                json={"tenant_slug": slug, "email": "owner@test.example", "password": "wrong"},
            )
            unknown_email = client.post(
                "/api/v1/auth/login",
                json={
                    "tenant_slug": slug,
                    "email": "nobody@test.example",
                    "password": "adminpass",
                },
            )
            assert wrong_password.status_code == unknown_email.status_code == 401
            assert wrong_password.json() == unknown_email.json()
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_login_fails_for_unknown_tenant_slug_with_the_same_error() -> None:
    with TestClient(create_app(Settings(app_env="test"))) as client:
        r = client.post(
            "/api/v1/auth/login",
            json={
                "tenant_slug": "no-such-tenant-slug",
                "email": "x@x.com",
                "password": "x",
            },
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "authentication_required"


# ── tenant config ────────────────────────────────────────────────────────


@requires_services
def test_get_tenant_returns_the_signed_in_users_own_tenant() -> None:
    engine = _sync_engine()
    slug = f"auth-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            token = _login(client, slug=slug, email="owner@test.example", password="adminpass")[
                "access_token"
            ]
            r = client.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
            assert r.json()["slug"] == slug
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_patch_tenant_settings_deep_merges_over_existing_value() -> None:
    """Route-level regression test for the deep-merge bug (see
    test_util.py for the unit-level coverage of deep_merge itself)."""
    engine = _sync_engine()
    slug = f"auth-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            token = _login(client, slug=slug, email="owner@test.example", password="adminpass")[
                "access_token"
            ]
            headers = {"Authorization": f"Bearer {token}"}
            r = client.patch(
                "/api/v1/tenants/me",
                headers=headers,
                json={"settings": {"crm": {"enabled": False}}},
            )
            assert r.status_code == 200
            crm = r.json()["settings"]["crm"]
            assert crm["enabled"] is False
            assert crm["deal_min_score"] == 30
            assert crm["stage_by_outcome"] == {"default": "new"}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_viewer_cannot_patch_tenant_settings() -> None:
    engine = _sync_engine()
    slug = f"auth-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            token = _login(client, slug=slug, email="viewer@test.example", password="viewpass")[
                "access_token"
            ]
            headers = {"Authorization": f"Bearer {token}"}
            get_r = client.get("/api/v1/tenants/me", headers=headers)
            patch_r = client.patch(
                "/api/v1/tenants/me", headers=headers, json={"timezone": "America/Chicago"}
            )
            assert get_r.status_code == 200
            assert patch_r.status_code == 403
            assert patch_r.json()["error"]["code"] == "forbidden"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_no_bearer_token_is_rejected() -> None:
    with TestClient(create_app(Settings(app_env="test"))) as client:
        r = client.get("/api/v1/tenants/me")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "authentication_required"


# ── agent config ─────────────────────────────────────────────────────────


@requires_services
def test_patch_agent_config_creates_a_new_version_preserving_unset_fields() -> None:
    engine = _sync_engine()
    slug = f"auth-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            token = _login(client, slug=slug, email="owner@test.example", password="adminpass")[
                "access_token"
            ]
            headers = {"Authorization": f"Bearer {token}"}
            before = client.get("/api/v1/tenants/me/agent-config", headers=headers).json()
            assert before["version"] == 1

            after = client.patch(
                "/api/v1/tenants/me/agent-config",
                headers=headers,
                json={"first_message": "Hi, thanks for calling!"},
            ).json()
            assert after["version"] == 2
            assert after["is_active"] is True
            assert after["first_message"] == "Hi, thanks for calling!"
            # Untouched fields carried over from version 1, not cleared.
            assert after["system_prompt"] == before["system_prompt"]
            assert after["qualification"] == before["qualification"]

            # The old version is no longer active.
            current = client.get("/api/v1/tenants/me/agent-config", headers=headers).json()
            assert current["version"] == 2
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()
