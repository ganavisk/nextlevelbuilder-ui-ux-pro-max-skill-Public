"""Tenant configuration is the product: onboarding a business is a YAML file.

These tests hold every example tenant to the rules the code relies on, so a
typo in a config is caught here instead of on a live call, and they check how
config shapes the agent (prompt and tools) for a non-dental business.
"""

from pathlib import Path

import pytest
import yaml

from app.modules.conversation.tools import TOOL_SCHEMAS, booking_fields, tool_schemas_for
from app.modules.scheduling.service import bay_count
from app.modules.tenants.models import AgentConfig, Tenant
from app.modules.tenants.service import assemble_system_prompt

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "tenants"
CONFIGS = sorted(CONFIG_DIR.glob("*.yaml"))
VERTICALS = {"dental", "legal", "auto_repair", "other"}  # migration 0008


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _tenant_from(doc: dict) -> Tenant:
    """The same mapping scripts/seed_demo.py performs."""
    t = doc["tenant"]
    return Tenant(
        slug=t["slug"],
        name=t["name"],
        vertical=t["vertical"],
        timezone=t.get("timezone", "America/New_York"),
        business_hours=t.get("business_hours", {}),
        settings={
            "forbidden_topics": doc["agent"].get("forbidden_topics", []),
            "scheduling": doc.get("scheduling", {}),
        },
    )


def _agent_from(doc: dict) -> AgentConfig:
    return AgentConfig(
        system_prompt=doc["agent"]["persona"],
        first_message=doc["agent"]["first_message"],
        qualification=doc.get("qualification", []),
        escalation_policy=doc.get("escalation", {}),
    )


def test_there_is_an_auto_repair_example() -> None:
    assert any(_load(p)["tenant"]["vertical"] == "auto_repair" for p in CONFIGS)


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_every_example_tenant_is_valid(path: Path) -> None:
    doc = _load(path)
    tenant = _tenant_from(doc)
    assert tenant.vertical in VERTICALS

    scheduling = doc.get("scheduling", {})
    for service in scheduling.get("services", []):
        assert service["name"]
        assert int(service["duration_minutes"]) > 0
    configured_fields = scheduling.get("booking_fields", [])
    assert len(booking_fields(tenant)) == len(configured_fields), "a booking field was dropped"

    keys = [q["key"] for q in doc.get("qualification", [])]
    assert len(keys) == len(set(keys)), "duplicate qualification keys"
    for question in doc.get("qualification", []):
        if question.get("type") == "choice":
            options = {str(o).lower() for o in question["options"]}
            for answer in question.get("scoring") or {}:
                assert str(answer).lower() in options, f"{question['key']} scores {answer!r}"

    for day, intervals in tenant.business_hours.items():
        for opens, closes in intervals:
            assert opens < closes, f"{day} closes before it opens"


def test_auto_shop_runs_three_bays_and_collects_vehicle_details() -> None:
    doc = _load(CONFIG_DIR / "example-auto.yaml")
    tenant = _tenant_from(doc)
    assert bay_count(tenant) == 3

    book = next(t for t in tool_schemas_for(tenant) if t["function"]["name"] == "book_appointment")
    properties = book["function"]["parameters"]["properties"]
    assert {"vehicle", "mileage", "concern"} <= set(properties)
    assert "Oil change" in properties["service"]["description"]
    # Extra fields are optional: the model must still be able to book without them.
    required = set(book["function"]["parameters"]["required"])
    assert required == {"service", "starts_at", "customer_name"}


def test_the_shared_prompt_is_not_dental_specific() -> None:
    doc = _load(CONFIG_DIR / "example-auto.yaml")
    prompt = assemble_system_prompt(_tenant_from(doc), _agent_from(doc))
    assert "practice" not in prompt.lower()
    assert "insurance" not in prompt.lower().split("you must never discuss")[0]
    assert "Approximate mileage" in prompt  # booking fields are asked for
    assert "record exactly one of: drivable, needs_tow" in prompt  # choice options shown


def test_a_booking_field_that_repeats_a_question_is_not_asked_twice() -> None:
    """The auto shop asks for the vehicle and the concern as qualification
    questions, and needs both on the appointment. The caller hears each once."""
    doc = _load(CONFIG_DIR / "example-auto.yaml")
    prompt = assemble_system_prompt(_tenant_from(doc), _agent_from(doc))
    booking = prompt[prompt.index("Before calling book_appointment") :]
    booking = booking[: booking.index("\n\n")] if "\n\n" in booking else booking
    assert "Approximate mileage" in booking
    assert "Year, make and model" not in booking  # already a question
    assert "For vehicle, concern, reuse the caller's answer" in booking
    assert "never ask for something they have already told you" in prompt
    assert "skip a question that clearly doesn't apply" in prompt


def test_tenants_without_booking_fields_get_the_standard_tools() -> None:
    tenant = Tenant(settings={})
    assert tool_schemas_for(tenant) == TOOL_SCHEMAS


def test_malformed_or_clashing_booking_fields_are_dropped() -> None:
    tenant = Tenant(
        settings={
            "scheduling": {
                "booking_fields": [
                    {"key": "vehicle", "description": "Vehicle"},
                    {"key": "service", "description": "would shadow the real service field"},
                    {"key": "Bad Key!", "description": "not a safe property name"},
                    {"description": "no key at all"},
                ]
            }
        }
    )
    assert [f["key"] for f in booking_fields(tenant)] == ["vehicle"]


def test_customising_tools_never_mutates_the_shared_schemas() -> None:
    before = repr(TOOL_SCHEMAS)
    tool_schemas_for(_tenant_from(_load(CONFIG_DIR / "example-auto.yaml")))
    assert repr(TOOL_SCHEMAS) == before
