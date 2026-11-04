"""Scripted Test Lab scenarios: load them and judge a finished conversation.

A scenario is a few caller lines played as a simulated call (see
`config/scenarios/*.yaml` and `scripts/lab_scenarios.py`). This module is
the pure part: what a scenario expects, and whether a conversation met it.
Judgement is on what the agent *did* (its tool calls), plus specific facts it
had to say (`reply_mentions`), never on overall wording, which varies run to
run.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CALLBACK_TOOL = "request_callback"


@dataclass(frozen=True)
class Expectation:
    callback: bool
    reason_starts_with: str | None = None
    reply_mentions: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    turns: tuple[str, ...]
    expect: Expectation
    trigger: str | None = None


@dataclass
class Turn:
    """One caller line and what came back (shape of the Test Lab TurnOut)."""

    said: str
    reply: str
    tool_events: list[dict[str, Any]] = field(default_factory=list)


def load(path: Path) -> tuple[str, list[Scenario]]:
    """(tenant slug, scenarios) from a scenarios YAML file."""
    doc = yaml.safe_load(path.read_text())
    scenarios = []
    for raw in doc["scenarios"]:
        expect = raw["expect"]
        scenarios.append(
            Scenario(
                name=raw["name"],
                trigger=raw.get("trigger"),
                turns=tuple(raw["turns"]),
                expect=Expectation(
                    callback=bool(expect["callback"]),
                    reason_starts_with=expect.get("reason_starts_with"),
                    reply_mentions=tuple(expect.get("reply_mentions", ())),
                    tools=tuple(expect.get("tools", ())),
                ),
            )
        )
    return str(doc["tenant"]), scenarios


def evaluate(scenario: Scenario, turns: list[Turn]) -> list[str]:
    """Why the conversation fails the scenario; empty if it passes."""
    events = [e for t in turns for e in t.tool_events]
    called = [e["tool"] for e in events]
    callbacks = [e for e in events if e["tool"] == CALLBACK_TOOL]
    replies = " ".join(t.reply for t in turns)
    problems = []

    if scenario.expect.callback and not callbacks:
        problems.append("expected a callback request; none was made")
    if not scenario.expect.callback and callbacks:
        reason = callbacks[0].get("arguments", {}).get("reason", "")
        problems.append(f"escalated when it should have handled it (reason: {reason!r})")
    if scenario.expect.reason_starts_with and callbacks:
        prefix = scenario.expect.reason_starts_with
        reasons = [str(c.get("arguments", {}).get("reason", "")) for c in callbacks]
        if not any(r.strip().upper().startswith(prefix.upper()) for r in reasons):
            problems.append(f"callback reason doesn't start with {prefix!r}: {reasons!r}")
    for fact in scenario.expect.reply_mentions:
        if fact.lower() not in replies.lower():
            problems.append(f"never said {fact!r}")
    for tool in scenario.expect.tools:
        if tool not in called:
            problems.append(f"expected a {tool} call; tools used: {called or 'none'}")
    return problems
