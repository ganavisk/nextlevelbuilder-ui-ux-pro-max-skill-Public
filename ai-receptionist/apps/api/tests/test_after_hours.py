"""Breakdown calls and the after-hours promise.

A stranded caller at 9 pm used to hear "a service advisor will call you back
within 30 minutes during shop hours", which in practice meant tomorrow
morning. The promise now follows the business hours, and the auto shop's
safety script puts the person before the car.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from app.modules.escalation.service import callback_promise_for
from app.modules.scheduling.availability import next_opening

DENVER = ZoneInfo("America/Denver")
SHOP_HOURS = {
    "mon": [["07:30", "18:00"]],
    "tue": [["07:30", "18:00"]],
    "wed": [["07:30", "18:00"]],
    "thu": [["07:30", "18:00"]],
    "fri": [["07:30", "18:00"]],
    "sat": [["08:00", "14:00"]],
}
POLICY = {
    "callback_promise": "A service advisor will call you back within 30 minutes during shop hours.",
    "after_hours_promise": (
        "The shop is closed right now, so a service advisor will call you back when we "
        "open, {next_open}."
    ),
}
AUTO_YAML = Path(__file__).resolve().parents[3] / "config" / "tenants" / "example-auto.yaml"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """November 2026 in Denver: the 2nd is a Monday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=DENVER)


def _opens(now: datetime) -> datetime | None:
    return next_opening(SHOP_HOURS, "America/Denver", now)


# ── when does it open ───────────────────────────────────────────────────


def test_open_now_has_no_next_opening() -> None:
    assert _opens(_at(3, 10)) is None
    assert _opens(_at(3, 7, 30)) is None  # opening minute counts as open


def test_before_opening_it_opens_today() -> None:
    assert _opens(_at(3, 6, 15)) == _at(3, 7, 30)


def test_at_closing_time_it_is_already_closed() -> None:
    assert _opens(_at(3, 18)) == _at(4, 7, 30)


def test_friday_evening_opens_saturday_and_saturday_afternoon_skips_sunday() -> None:
    assert _opens(_at(6, 21)) == _at(7, 8)  # Saturday's shorter hours
    assert _opens(_at(7, 15)) == _at(9, 7, 30)  # closed Sunday: Monday


def test_a_lunch_break_is_closed() -> None:
    split = {"tue": [["08:00", "12:00"], ["13:00", "17:00"]]}
    assert next_opening(split, "America/Denver", _at(3, 12, 30)) == _at(3, 13)


def test_no_hours_configured_cannot_be_judged_closed() -> None:
    assert next_opening({}, "America/Denver", _at(3, 23)) is None


# ── what the caller is told ─────────────────────────────────────────────


def _promise(now: datetime, policy=POLICY) -> str:
    return callback_promise_for(policy, SHOP_HOURS, "America/Denver", now)


def test_during_hours_the_usual_promise_stands() -> None:
    assert _promise(_at(3, 10)) == POLICY["callback_promise"]


def test_at_night_the_caller_hears_when_the_shop_really_opens() -> None:
    assert _promise(_at(3, 21)) == (
        "The shop is closed right now, so a service advisor will call you back when we "
        "open, tomorrow at 7:30 am."
    )


def test_saturday_evening_names_monday() -> None:
    assert _promise(_at(7, 19)).endswith("when we open, Monday at 7:30 am.")


def test_early_morning_says_today() -> None:
    assert _promise(_at(3, 5)).endswith("when we open, today at 7:30 am.")


def test_a_business_without_an_after_hours_promise_is_unchanged() -> None:
    plain = {"callback_promise": "We will call you back."}
    assert _promise(_at(3, 21), plain) == "We will call you back."
    assert callback_promise_for({}, SHOP_HOURS, "America/Denver", _at(3, 21)) == (
        "Someone from the team will call you back."
    )


async def test_the_callback_tool_uses_the_clock(monkeypatch) -> None:
    """Wired through the tool the agent calls: same request, two clocks."""
    import app.modules.conversation.tools as tools
    from app.modules.tenants.models import AgentConfig, Tenant

    saved: list[dict] = []

    async def fake_create(**kwargs):
        saved.append(kwargs)
        return "id"

    monkeypatch.setattr(tools, "create_callback_request", fake_create)
    tenant = Tenant(
        slug="s",
        name="Shop",
        vertical="auto_repair",
        timezone="America/Denver",
        business_hours=SHOP_HOURS,
        settings={},
        status="active",
    )
    config = AgentConfig(
        version=1,
        is_active=True,
        system_prompt="",
        first_message="",
        qualification=[],
        escalation_policy=POLICY,
    )
    executor = tools.ToolExecutor(None, None)  # type: ignore[arg-type]

    class Clock(datetime):
        current = _at(3, 10)

        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls.current.astimezone(tz)

    monkeypatch.setattr(tools, "datetime", Clock)

    async def call() -> str:
        return await executor.dispatch(
            tenant=tenant,
            call_id=None,
            caller_e164="+15550009999",
            agent_config=config,
            tool_name="request_callback",
            arguments={"reason": "URGENT: brakes grinding"},
        )

    assert await call() == POLICY["callback_promise"]
    Clock.current = _at(3, 22)
    assert (await call()).endswith("when we open, tomorrow at 7:30 am.")
    assert len(saved) == 2  # the callback is queued either way


# ── the prompt and the auto shop's script ───────────────────────────────


def test_the_prompt_defers_to_the_tool_for_when_they_will_hear_back() -> None:
    """The prompt used to quote the fixed promise for the agent to say, which
    would override the after-hours answer."""
    from app.modules.tenants.service import assemble_system_prompt
    from tests.test_tenant_config import _agent_from, _tenant_from

    doc = yaml.safe_load(AUTO_YAML.read_text())
    prompt = assemble_system_prompt(_tenant_from(doc), _agent_from(doc))
    assert "within 30 minutes" not in prompt
    assert "tell them what the tool says about when they'll hear back" in prompt


def test_the_auto_shop_puts_the_person_first_and_gives_the_tow_line() -> None:
    doc = yaml.safe_load(AUTO_YAML.read_text())
    persona = " ".join(doc["agent"]["persona"].split())
    # Order matters: safety, then 911 for fire risk, then the tow line, then the callback.
    order = [
        persona.index("somewhere safe"),
        persona.index("call 911"),
        persona.index("5 5 5, 0 1 4, 2 2 0 0"),
        persona.index('"URGENT"'),
    ]
    assert order == sorted(order)
    knowledge = " ".join(k["content"] for k in doc["knowledge"])
    assert "555-014-2200" in knowledge and "24 hours" in knowledge
    assert "{next_open}" in doc["escalation"]["after_hours_promise"]
