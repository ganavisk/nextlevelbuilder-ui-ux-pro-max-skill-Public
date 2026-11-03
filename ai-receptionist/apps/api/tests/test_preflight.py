"""`make check-keys`, driven against fake providers.

What matters is that each wrong setting is named precisely (so a broken test
call can be fixed without guessing), that only the settings a call really
needs block it, and that no key is ever printed.
"""

import httpx
import pytest

from app.core.config import Settings
from app.preflight import exit_code, render, run_checks

VAPI_KEY = "vapi-0123456789-secret-abcd"
ANTHROPIC_KEY = "sk-ant-api03-very-secret-wxyz"
VOYAGE_KEY = "pa-voyage-secret-9876"
PUBLIC_URL = "https://abc123.ngrok.app"


def _settings(**overrides) -> Settings:
    values = {
        "vapi_api_key": VAPI_KEY,
        "vapi_webhook_secret": "w" * 64,
        "anthropic_api_key": ANTHROPIC_KEY,
        "public_webhook_base_url": PUBLIC_URL,
        **overrides,
    }
    return Settings(_env_file=None, app_env="test", **values)


def _providers(
    *,
    vapi: int = 200,
    anthropic: int = 200,
    missing_models: tuple[str, ...] = (),
    tunnel: str = "api",
    voyage: int = 200,
):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.vapi.ai":
            assert request.headers["authorization"] == f"Bearer {VAPI_KEY}"
            return httpx.Response(vapi, json=[])
        if host == "api.anthropic.com":
            assert request.headers["x-api-key"] == ANTHROPIC_KEY
            if anthropic != 200:
                return httpx.Response(anthropic, json={"error": {}})
            model = path.rsplit("/", 1)[-1]
            return httpx.Response(404 if model in missing_models else 200, json={"id": model})
        if host == "abc123.ngrok.app":
            if tunnel == "down":
                raise httpx.ConnectError("tunnel offline")
            if tunnel == "ngrok-page":
                html = {"content-type": "text/html"}
                return httpx.Response(200, text="<html>ngrok</html>", headers=html)
            return httpx.Response(200, json={"status": "ok", "version": "0.1.0"})
        if host == "api.voyageai.com":
            return httpx.Response(voyage, json={"data": []})
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _by_name(checks):
    return {c.name: c for c in checks}


def test_everything_valid_is_ready_to_provision() -> None:
    checks = run_checks(_settings(), _providers())
    assert exit_code(checks) == 0
    assert all(c.status == "ok" for c in checks if c.required)
    assert "Ready to provision" in render(checks)


def test_a_rejected_vapi_key_is_named_and_blocks() -> None:
    checks = run_checks(_settings(), _providers(vapi=401))
    vapi = _by_name(checks)["Vapi API key"]
    assert vapi.status == "fail"
    assert "rejected (HTTP 401)" in vapi.detail
    assert exit_code(checks) == 1


def test_a_rejected_anthropic_key_is_reported_once_not_per_model() -> None:
    checks = run_checks(_settings(), _providers(anthropic=401))
    anthropic = [c for c in checks if "Anthropic" in c.name or "Claude" in c.name]
    assert len(anthropic) == 1
    assert anthropic[0].status == "fail"


def test_a_model_name_the_key_cannot_use_is_caught() -> None:
    """A valid key with a mistyped LLM_MODEL_PRIMARY would otherwise only
    fail on the first question a caller asks."""
    checks = run_checks(
        _settings(llm_model_primary="claude-sonnet-typo"),
        _providers(missing_models=("claude-sonnet-typo",)),
    )
    by_name = _by_name(checks)
    assert by_name["Anthropic API key"].status == "ok"
    assert by_name["Claude model for in-call"].status == "fail"
    assert "claude-sonnet-typo" in by_name["Claude model for in-call"].detail
    assert by_name["Claude model for summaries"].status == "ok"
    assert exit_code(checks) == 1


def test_a_tunnel_that_is_down_says_so() -> None:
    check = _by_name(run_checks(_settings(), _providers(tunnel="down")))["Public URL to this API"]
    assert check.status == "fail"
    assert "tunnel" in check.detail


def test_a_url_answered_by_something_other_than_this_api_fails() -> None:
    """A 200 from an ngrok interstitial or the wrong local port is not the API."""
    checks = run_checks(_settings(), _providers(tunnel="ngrok-page"))
    check = _by_name(checks)["Public URL to this API"]
    assert check.status == "fail"
    assert "make dev" in check.detail


def test_plain_http_public_url_is_refused_without_a_request() -> None:
    check = _by_name(
        run_checks(_settings(public_webhook_base_url="http://abc123.ngrok.app"), _providers())
    )["Public URL to this API"]
    assert check.status == "fail"
    assert "https" in check.detail


@pytest.mark.parametrize(
    ("setting", "check_name"),
    [
        ("vapi_api_key", "Vapi API key"),
        ("vapi_webhook_secret", "Vapi webhook secret"),
        ("anthropic_api_key", "Anthropic API key"),
        ("public_webhook_base_url", "Public URL to this API"),
    ],
)
def test_each_required_setting_blocks_when_missing(setting: str, check_name: str) -> None:
    checks = run_checks(_settings(**{setting: ""}), _providers())
    assert _by_name(checks)[check_name].status == "fail"
    assert exit_code(checks) == 1


def test_optional_settings_never_block_a_test_call() -> None:
    checks = run_checks(_settings(voyage_api_key=""), _providers())
    by_name = _by_name(checks)
    assert by_name["Voyage API key"].status == "warn"
    assert "fallback" in by_name["Voyage API key"].detail
    assert by_name["Google Calendar OAuth"].status == "skip"
    assert exit_code(checks) == 0


def test_a_rejected_optional_key_fails_its_line_but_not_the_run() -> None:
    checks = run_checks(_settings(voyage_api_key=VOYAGE_KEY), _providers(voyage=401))
    assert _by_name(checks)["Voyage API key"].status == "fail"
    assert exit_code(checks) == 0


def test_half_configured_oauth_is_flagged() -> None:
    checks = run_checks(_settings(google_oauth_client_id="id-only"), _providers())
    assert _by_name(checks)["Google Calendar OAuth"].status == "fail"


def test_an_invalid_encryption_key_is_flagged() -> None:
    checks = run_checks(_settings(credentials_encryption_key="not-a-fernet-key"), _providers())
    assert _by_name(checks)["Credentials encryption key"].status == "fail"


def test_no_key_is_ever_printed() -> None:
    settings = _settings(voyage_api_key=VOYAGE_KEY)
    for providers in (_providers(), _providers(vapi=401, anthropic=403, voyage=401)):
        output = render(run_checks(settings, providers))
        for secret in (VAPI_KEY, ANTHROPIC_KEY, VOYAGE_KEY, "w" * 64):
            assert secret not in output
    assert "...abcd" in render(run_checks(settings, _providers()))  # the tail, to tell keys apart
