import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import ValidationFailedError, WebhookSignatureError
from app.modules.telephony.providers.base import CallEventType
from app.modules.telephony.providers.vapi import VapiProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def provider() -> VapiProvider:
    return VapiProvider(Settings(_env_file=None, vapi_webhook_secret="test-secret"))


def test_verify_accepts_correct_secret(provider: VapiProvider) -> None:
    provider.verify_webhook(b"{}", {"x-vapi-secret": "test-secret"})
    provider.verify_webhook(b"{}", {"X-Vapi-Secret": "test-secret"})  # header case


def test_verify_rejects_wrong_or_missing_secret(provider: VapiProvider) -> None:
    with pytest.raises(WebhookSignatureError):
        provider.verify_webhook(b"{}", {"x-vapi-secret": "wrong"})
    with pytest.raises(WebhookSignatureError):
        provider.verify_webhook(b"{}", {})


def test_verify_rejects_when_unconfigured() -> None:
    unconfigured = VapiProvider(Settings(_env_file=None, vapi_webhook_secret=""))
    with pytest.raises(WebhookSignatureError):
        unconfigured.verify_webhook(b"{}", {"x-vapi-secret": ""})


def test_parse_call_started(provider: VapiProvider) -> None:
    event = provider.parse_event(load("vapi_call_started.json"))
    assert event.event_type is CallEventType.CALL_STARTED
    assert event.vendor_call_id == "vapi-call-0001"
    assert event.to_number == "+15550100001"
    assert event.from_number == "+15550002222"


def test_parse_end_of_call_report(provider: VapiProvider) -> None:
    event = provider.parse_event(load("vapi_end_of_call_report.json"))
    assert event.event_type is CallEventType.CALL_ENDED
    assert event.payload["artifact"]["recordingUrl"].endswith(".wav")
    assert event.payload["cost"] == 0.37


def test_parse_tool_calls(provider: VapiProvider) -> None:
    event = provider.parse_event(load("vapi_tool_calls.json"))
    assert event.event_type is CallEventType.TOOL_CALL
    assert event.payload["toolCallList"][0]["name"] == "check_availability"


def test_parse_unknown_type_rejected(provider: VapiProvider) -> None:
    with pytest.raises(ValidationFailedError):
        provider.parse_event({"message": {"type": "something-new"}})


def test_extract_tool_calls(provider: VapiProvider) -> None:
    event = provider.parse_event(load("vapi_tool_calls.json"))
    calls = provider.extract_tool_calls(event)
    assert len(calls) == 1
    assert calls[0].id == "toolcall-0001"
    assert calls[0].name == "check_availability"
    assert calls[0].arguments == {"service": "Cleaning & check-up", "date": "2026-07-20"}


def test_extract_tool_calls_empty_when_none_present(provider: VapiProvider) -> None:
    event = provider.parse_event(load("vapi_call_started.json"))
    assert provider.extract_tool_calls(event) == []


def test_format_tool_results(provider: VapiProvider) -> None:
    body = provider.format_tool_results(
        [("toolcall-0001", "3 slots available"), ("toolcall-0002", "Got it, thank you.")]
    )
    assert body == {
        "results": [
            {"toolCallId": "toolcall-0001", "result": "3 slots available"},
            {"toolCallId": "toolcall-0002", "result": "Got it, thank you."},
        ]
    }
