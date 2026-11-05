"""Error reporting must not become a second place call data leaks to.

The end-to-end test drives a real request that fails with caller details
everywhere the SDK normally looks — exception message, request body, headers,
query string, a local variable in the failing frame — and asserts that none of
it is in what would be sent.
"""

import json

import pytest
import sentry_sdk
from fastapi import Request
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app.core.config import Settings
from app.core.errors import AuthenticationError, IntegrationError, NotFoundError
from app.core.observability import before_send, init_sentry, scrub_text
from app.main import create_app

PHONE = "+13128471928"
EMAIL = "dana.okafor@example.com"
TRANSCRIPT = "my crown fell off and I take warfarin"
PASSWORD = "hunter2-correct-horse"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlLXZhbHVlLWhlcmU"


class CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


@pytest.fixture
def sentry():
    transport = CapturingTransport()
    settings = Settings(
        _env_file=None, app_env="test", sentry_dsn="https://k@o0.ingest.sentry.io/1"
    )
    assert init_sentry(settings, service="api", transport=transport)
    yield transport
    sentry_sdk.init()  # disable again so later tests don't report


def test_disabled_without_a_dsn() -> None:
    assert init_sentry(Settings(_env_file=None, app_env="test"), service="api") is False


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        (f"call from {PHONE} failed", PHONE),
        ("caller +1 (312) 847-1928 hung up", "847-1928"),
        ("caller (312) 847-1928 hung up", "847-1928"),
        (f"no confirmation sent to {EMAIL}", EMAIL),
        (f"Authorization: Bearer {TOKEN}", TOKEN),
        (f"token {TOKEN} rejected", TOKEN),
    ],
)
def test_scrub_text_masks_personal_data(raw: str, must_not_contain: str) -> None:
    assert must_not_contain not in scrub_text(raw)


def test_scrub_text_leaves_ordinary_text_alone() -> None:
    text = "tool_dispatch_failed for tenant 7f3a at step 3 after 2.5s"
    assert scrub_text(text) == text


def test_expected_client_errors_are_not_reported() -> None:
    for exc in (AuthenticationError("bad"), NotFoundError("no call")):
        assert before_send({"message": "x"}, {"exc_info": (type(exc), exc, None)}) is None


def test_server_side_failures_are_reported() -> None:
    exc = IntegrationError("hubspot 503")
    assert before_send({"message": "x"}, {"exc_info": (type(exc), exc, None)}) is not None


def test_request_payload_and_user_are_removed_and_text_scrubbed() -> None:
    event = {
        "request": {
            "url": "http://x/api/v1/auth/login",
            "method": "POST",
            "data": {"password": PASSWORD},
            "headers": {"authorization": f"Bearer {TOKEN}"},
            "cookies": {"rx_session": TOKEN},
            "query_string": "code=abc&state=def",
        },
        "user": {"email": EMAIL},
        "breadcrumbs": {"values": [{"message": f"sent to {PHONE}"}]},
        "exception": {"values": [{"type": "X", "value": f"failed for {EMAIL}"}]},
    }
    out = json.dumps(before_send(event, {}))
    for secret in (PASSWORD, TOKEN, EMAIL, PHONE, "code=abc"):
        assert secret not in out
    assert "http://x/api/v1/auth/login" in out  # the useful part survives


def test_a_failing_request_reports_no_caller_data(sentry: CapturingTransport) -> None:
    app = create_app(Settings(_env_file=None, app_env="test"))

    async def boom(request: Request) -> None:
        transcript = TRANSCRIPT  # a local in the failing frame
        raise RuntimeError(f"could not summarize call from {PHONE} ({EMAIL}): {len(transcript)}")

    app.add_api_route("/boom", boom, methods=["POST"])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/boom?code=oauth-code-123",
            json={"password": PASSWORD, "turns": [{"text": TRANSCRIPT}]},
            headers={"Authorization": f"Bearer {TOKEN}", "Cookie": f"rx_session={TOKEN}"},
        )
        # An expected 401 on the same app must not produce an event.
        client.get("/api/v1/tenants/me")
    assert response.status_code == 500

    assert len(sentry.events) == 1, [e.get("exception") for e in sentry.events]
    event = sentry.events[0]
    payload = json.dumps(event)
    for secret in (PHONE, EMAIL, TRANSCRIPT, PASSWORD, TOKEN, "oauth-code-123", "847-1928"):
        assert secret not in payload, f"{secret!r} leaked into the Sentry event"
    assert event["tags"]["service"] == "api"
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
