"""Provisioning contract tests against a mock HTTP transport.

These don't need live Vapi credentials, but they lock down the parts that
silently break a real deployment: the server URL + secret the vendor needs
in order to reach us, the tool schemas the agent can call, and
create-vs-update idempotency so re-provisioning doesn't spawn duplicate
assistants.
"""

import json
import uuid

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.modules.conversation.tools import TOOL_SCHEMAS
from app.modules.telephony.providers.base import AgentDefinition
from app.modules.telephony.providers.vapi import VapiProvider


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "vapi_api_key": "vapi-key",
        "vapi_webhook_secret": "hook-secret",
        "public_webhook_base_url": "https://api.example.com",
        "twilio_account_sid": "AC123",
        "twilio_auth_token": "twilio-token",
    }
    return Settings(**{**base, **overrides})


def _definition(**overrides) -> AgentDefinition:
    base = {
        "tenant_id": uuid.uuid4(),
        "system_prompt": "You are a receptionist.",
        "first_message": "Thanks for calling!",
        "voice_id": "voice-abc",
        "timezone": "America/Denver",
        "tools": TOOL_SCHEMAS,
        "max_duration_seconds": 900,
    }
    return AgentDefinition(**{**base, **overrides})


def _mock(provider: VapiProvider, handler) -> list[httpx.Request]:
    """Route the provider's HTTP calls to `handler`, recording requests."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url="https://api.vapi.ai", transport=httpx.MockTransport(_record)
    )
    return seen


async def test_new_agent_payload_carries_server_url_secret_and_tools() -> None:
    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-new"}))

    agent_id = await provider.sync_agent(_definition())

    assert agent_id == "asst-new"
    assert seen[0].method == "POST"
    body = json.loads(seen[0].content)
    # Without these the vendor cannot reach us and no tool call ever arrives.
    assert body["server"]["url"] == "https://api.example.com/webhooks/voice/vapi"
    assert body["server"]["secret"] == "hook-secret"
    assert "tool-calls" in body["serverMessages"]
    assert "end-of-call-report" in body["serverMessages"]
    tool_names = {t["function"]["name"] for t in body["model"]["tools"]}
    assert {
        "answer_faq",
        "record_qualification_answer",
        "check_availability",
        "book_appointment",
        "request_callback",
    } == tool_names
    assert body["model"]["messages"][0]["content"].startswith("You are a receptionist.")
    assert body["voice"]["voiceId"] == "voice-abc"


async def test_elevenlabs_voice_carries_provider_and_model() -> None:
    """A voice id without its provider silently falls back to Vapi's default
    voice — the provider field is what actually selects ElevenLabs."""
    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-1"}))

    await provider.sync_agent(
        _definition(
            voice_id="21m00Tcm4TlvDq8ikWAM",
            voice_provider="11labs",
            voice_model="eleven_turbo_v2_5",
        )
    )

    assert json.loads(seen[0].content)["voice"] == {
        "voiceId": "21m00Tcm4TlvDq8ikWAM",
        "provider": "11labs",
        "model": "eleven_turbo_v2_5",
    }


async def test_voice_provider_and_model_omitted_when_unset() -> None:
    """Tenants that haven't chosen a vendor must not pin one — an empty
    string would be an invalid provider, not 'use the default'."""
    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-1"}))

    await provider.sync_agent(_definition(voice_provider="", voice_model=""))

    assert json.loads(seen[0].content)["voice"] == {"voiceId": "voice-abc"}


async def test_no_voice_key_when_no_voice_configured() -> None:
    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-1"}))

    await provider.sync_agent(_definition(voice_id=""))

    assert "voice" not in json.loads(seen[0].content)


async def test_existing_agent_is_updated_not_duplicated() -> None:
    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-existing"}))

    agent_id = await provider.sync_agent(_definition(), existing_agent_id="asst-existing")

    assert agent_id == "asst-existing"
    assert [r.method for r in seen] == ["PATCH"]
    assert seen[0].url.path == "/assistant/asst-existing"


async def test_stale_agent_id_falls_back_to_create() -> None:
    """An assistant deleted in the dashboard must not wedge provisioning."""
    provider = VapiProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"id": "asst-recreated"})

    seen = _mock(provider, handler)
    agent_id = await provider.sync_agent(_definition(), existing_agent_id="asst-gone")

    assert agent_id == "asst-recreated"
    assert [r.method for r in seen] == ["PATCH", "POST"]


async def test_missing_public_url_fails_loudly() -> None:
    provider = VapiProvider(_settings(public_webhook_base_url=""))
    with pytest.raises(AppError, match="PUBLIC_WEBHOOK_BASE_URL"):
        await provider.sync_agent(_definition())


async def test_attach_number_imports_when_absent() -> None:
    provider = VapiProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": "num-new"})

    seen = _mock(provider, handler)
    number_id = await provider.attach_number("asst-1", "+15550100001")

    assert number_id == "num-new"
    assert [r.method for r in seen] == ["GET", "POST"]
    body = json.loads(seen[1].content)
    assert body["number"] == "+15550100001"
    assert body["assistantId"] == "asst-1"
    assert body["twilioAccountSid"] == "AC123"


async def test_attach_number_repoints_existing_instead_of_duplicating() -> None:
    provider = VapiProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "num-existing", "number": "+15550100001"}])
        return httpx.Response(200, json={"id": "num-existing"})

    seen = _mock(provider, handler)
    number_id = await provider.attach_number("asst-2", "+15550100001")

    assert number_id == "num-existing"
    assert [r.method for r in seen] == ["GET", "PATCH"]
    assert json.loads(seen[1].content) == {"assistantId": "asst-2"}


async def test_prompt_tells_every_call_todays_date_in_the_business_timezone() -> None:
    """The prompt is provisioned once but must give each call the current
    local date, or "tomorrow at 9" is booked on whatever date the model
    guesses. Vapi fills `now` (UTC) per call; the date filter converts it to
    the tenant's zone. These are the exact LiquidJS templates Vapi documents."""
    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-1"}))

    await provider.sync_agent(_definition(timezone="America/Denver"))

    prompt = json.loads(seen[0].content)["model"]["messages"][0]["content"]
    assert prompt.startswith("You are a receptionist.\n\n")
    assert (
        'Current local time at the business: {{"now" | date: "%A, %B %d, %Y, %I:%M %p", '
        '"America/Denver"}} (America/Denver).'
    ) in prompt
    assert '{{"now" | date: "%Y-%m-%d", "America/Denver"}}T09:00' in prompt


def _render_like_vapi(template: str, now) -> str:
    """Stand-in for Vapi's LiquidJS pass over the two date filters we emit."""
    import re
    from zoneinfo import ZoneInfo

    def sub(m: re.Match[str]) -> str:
        return now.astimezone(ZoneInfo(m.group(2))).strftime(m.group(1))

    return re.sub(r'\{\{"now" \| date: "([^"]+)", "([^"]+)"\}\}', sub, template)


async def test_rendered_prompt_matches_what_the_test_lab_tells_the_model() -> None:
    """Rendered for a given instant, the voice prompt's clock line is the
    same sentence the Test Lab uses, so rehearsals test what callers get.
    Late evening in Denver is already tomorrow in UTC: the local date must win."""
    from datetime import UTC, datetime

    from app.modules.telephony.providers.base import clock_instructions

    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-1"}))
    await provider.sync_agent(_definition(timezone="America/Denver"))
    template = json.loads(seen[0].content)["model"]["messages"][0]["content"]

    instant = datetime(2026, 11, 4, 4, 30, tzinfo=UTC)  # Tue Nov 3, 9:30 PM in Denver
    rendered = _render_like_vapi(template, instant)

    assert rendered.endswith(
        clock_instructions(
            now="Tuesday, November 03, 2026, 09:30 PM",
            today="2026-11-03",
            timezone="America/Denver",
        )
    )


async def test_an_invalid_timezone_is_refused_at_provisioning() -> None:
    """A bad zone name would make the agent quote the wrong local time on
    every call; fail loudly before anything is sent."""
    provider = VapiProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "asst-1"}))

    with pytest.raises(AppError, match="not an IANA timezone"):
        await provider.sync_agent(_definition(timezone="Mountain Time"))
    assert seen == []
