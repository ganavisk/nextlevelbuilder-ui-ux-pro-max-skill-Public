from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_healthz_reports_ok() -> None:
    app = create_app(Settings(app_env="test"))
    # Liveness must not touch external dependencies, so no DB/Redis is needed.
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_docs_disabled_in_production() -> None:
    app = create_app(Settings(app_env="production", secret_key="k" * 40))
    assert app.docs_url is None


def test_responses_are_hardened() -> None:
    with TestClient(create_app(Settings(app_env="test"))) as client:
        health = client.get("/healthz")
        assert health.headers["x-content-type-options"] == "nosniff"

        api = client.get("/api/v1/tenants/me")  # 401, but the headers still apply
        assert api.status_code == 401
        assert api.headers["cache-control"] == "no-store"
        assert api.headers["x-content-type-options"] == "nosniff"
