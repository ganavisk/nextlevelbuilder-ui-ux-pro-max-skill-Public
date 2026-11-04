"""The scenario judge, and the scenario files themselves."""

from pathlib import Path

import pytest
import yaml

from app.modules.testlab.scenarios import Expectation, Scenario, Turn, evaluate, load

CONFIG = Path(__file__).resolve().parents[3] / "config"
SCENARIO_FILES = sorted((CONFIG / "scenarios").glob("*.yaml"))


def _scenario(**expect) -> Scenario:
    return Scenario(name="s", turns=("hi",), expect=Expectation(**expect))


def _turn(reply: str = "", *events: tuple[str, dict]) -> Turn:
    return Turn(
        said="hi",
        reply=reply,
        tool_events=[{"tool": t, "arguments": a, "result": "ok"} for t, a in events],
    )


def test_a_callback_that_was_expected_and_made_passes() -> None:
    turns = [
        _turn("Let me take your details."),
        _turn("Done.", ("request_callback", {"reason": "x"})),
    ]
    assert evaluate(_scenario(callback=True), turns) == []


def test_a_missing_callback_fails() -> None:
    assert evaluate(_scenario(callback=True), [_turn("Sure, here's the answer.")]) == [
        "expected a callback request; none was made"
    ]


def test_escalating_a_control_fails_with_the_reason() -> None:
    problems = evaluate(
        _scenario(callback=False), [_turn("", ("request_callback", {"reason": "hours"}))]
    )
    assert problems == ["escalated when it should have handled it (reason: 'hours')"]


def test_urgent_prefix_and_spoken_facts_are_checked() -> None:
    scenario = _scenario(callback=True, reason_starts_with="URGENT", reply_mentions=("911",))
    calm = [_turn("Please stay safe.", ("request_callback", {"reason": "smoke from hood"}))]
    assert evaluate(scenario, calm) == [
        "callback reason doesn't start with 'URGENT': ['smoke from hood']",
        "never said '911'",
    ]
    good = [
        _turn(
            "Get away from the car and call 911.", ("request_callback", {"reason": "urgent: smoke"})
        )
    ]
    assert evaluate(scenario, good) == []


def test_a_required_tool_is_checked() -> None:
    assert evaluate(_scenario(callback=False, tools=("answer_faq",)), [_turn("We open at 8.")]) == [
        "expected a answer_faq call; tools used: none"
    ]


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda p: p.name)
def test_every_trigger_of_the_tenant_has_a_scenario(path: Path) -> None:
    """Adding a trigger to a tenant without a scenario for it fails here,
    and so does a scenario naming a trigger that no longer exists."""
    slug, scenarios = load(path)
    tenant = next(
        yaml.safe_load(p.read_text())
        for p in (CONFIG / "tenants").glob("*.yaml")
        if yaml.safe_load(p.read_text())["tenant"]["slug"] == slug
    )
    triggers = set(tenant["escalation"]["triggers"])
    covered = {s.trigger for s in scenarios if s.trigger}
    assert covered == triggers
    assert any(not s.expect.callback for s in scenarios), "needs controls that must not escalate"
