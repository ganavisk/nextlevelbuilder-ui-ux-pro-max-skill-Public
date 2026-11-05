"""Escalation triggers drive the agent: each business's situations for
handing a caller to the team are in the prompt, editable in Settings, and a
change reaches the phone agent through voice sync."""

import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from app.core.errors import ValidationFailedError
from app.modules.escalation.service import validate_triggers
from app.modules.tenants.service import assemble_system_prompt
from tests.conftest import requires_services
from tests.test_tenant_config import _agent_from, _tenant_from
from tests.test_voice_sync import _make_fixture

shop_fixture_factory = _make_fixture("shop_fixture", connected=True)

CONFIGS = sorted((Path(__file__).resolve().parents[3] / "config" / "tenants").glob("*.yaml"))


def _prompt(doc: dict) -> str:
    return assemble_system_prompt(_tenant_from(doc), _agent_from(doc))


# ── the prompt ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_every_example_business_gives_the_agent_its_triggers(path: Path) -> None:
    doc = yaml.safe_load(path.read_text())
    prompt = _prompt(doc)
    for trigger in doc["escalation"]["triggers"]:
        assert f"\n- {trigger}\n" in prompt, trigger
    assert "rather than handling the request yourself" in prompt


def test_without_triggers_only_the_universal_rule_applies() -> None:
    doc = yaml.safe_load(CONFIGS[0].read_text())
    doc["escalation"]["triggers"] = []
    prompt = _prompt(doc)
    assert (
        "If the caller asks for a human or you cannot help, use the request_callback tool. Collect"
        in prompt
    )
    assert "situations for this business" not in prompt


# ── validation ──────────────────────────────────────────────────────────


def test_triggers_are_tidied_and_deduplicated() -> None:
    assert validate_triggers(
        ["  Vehicle is unsafe\nto drive ", "", "vehicle is UNSAFE to drive", "Any invoice question"]
    ) == ["Vehicle is unsafe to drive", "Any invoice question"]


def test_an_empty_list_is_allowed() -> None:
    assert validate_triggers([]) == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (["x" * 161], "under 160 characters"),
        ([f"situation {i}" for i in range(11)], "Up to 10"),
        ("caller asks for a person", "must be a list"),
    ],
)
def test_unworkable_trigger_lists_are_refused(raw, message) -> None:
    with pytest.raises(ValidationFailedError, match=message):
        validate_triggers(raw)


# ── the API ─────────────────────────────────────────────────────────────


@pytest.fixture
def shop(shop_fixture):  # connected to the voice vendor, so changes queue a sync
    engine = shop_fixture["engine"]
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(shop_fixture["tenant_id"])},
        )
        conn.execute(
            text(
                "UPDATE agent_configs SET escalation_policy = CAST(:p AS jsonb) "
                "WHERE tenant_id = :t"
            ),
            {
                "p": json.dumps(
                    {
                        "mode": "voicemail_callback",
                        "triggers": ["caller asks for a person"],
                        "callback_promise": "We'll call you back soon.",
                    }
                ),
                "t": str(shop_fixture["tenant_id"]),
            },
        )
    return shop_fixture


@requires_services
def test_saving_triggers_updates_the_prompt_and_pushes_it_to_the_phone_agent(shop) -> None:
    headers = shop["login"]()
    r = shop["client"].put(
        "/api/v1/tenants/me/callback-triggers",
        headers=headers,
        json={"triggers": ["caller asks for a person", "question about an existing invoice"]},
    )
    assert r.status_code == 200, r.json()
    policy = r.json()["escalation_policy"]
    # The promise and mode the form doesn't show are kept.
    assert policy == {
        "mode": "voicemail_callback",
        "triggers": ["caller asks for a person", "question about an existing invoice"],
        "callback_promise": "We'll call you back soon.",
    }
    assert len(shop["jobs"]()) == 1  # the prompt changed: voice sync queued
    assert shop["status"](headers)["state"] == "updating"

    from tests.test_voice_sync import FakeVoiceProvider

    vendor = FakeVoiceProvider()
    shop["run_job"](vendor)
    pushed = vendor.synced[0][0].system_prompt
    assert "\n- question about an existing invoice\n" in pushed


@requires_services
def test_saving_the_same_triggers_pushes_nothing(shop) -> None:
    shop["client"].put(
        "/api/v1/tenants/me/callback-triggers",
        headers=shop["login"](),
        json={"triggers": ["  caller asks for a person "]},  # same after tidying
    )
    assert shop["jobs"]() == []


@requires_services
def test_the_raw_agent_config_api_tidies_and_checks_triggers_too(shop) -> None:
    headers = shop["login"]()
    bad = shop["client"].patch(
        "/api/v1/tenants/me/agent-config",
        headers=headers,
        json={"escalation_policy": {"triggers": ["x" * 200]}},
    )
    assert bad.status_code == 422


@requires_services
def test_viewers_cannot_change_triggers(shop) -> None:
    r = shop["client"].put(
        "/api/v1/tenants/me/callback-triggers",
        headers=shop["login"]("viewer"),
        json={"triggers": []},
    )
    assert r.status_code == 403
