"""Play scripted Test Lab calls against a running API and report what the
receptionist did.

Usage:
    python scripts/lab_scenarios.py example-auto [--repeat 3] [--only smoke]
    make lab-scenarios SCENARIOS=example-auto REPEAT=3

Each scenario in config/scenarios/<name>.yaml is played as a simulated call
through the Test Lab API: the tenant's real prompt, tools and model, with no
texts or emails sent. Every session is discarded afterwards, which removes
any callbacks or bookings it made. Models vary from run to run, so use
--repeat to see a pass rate rather than a single verdict.

Signs in as a dashboard admin: LAB_EMAIL / LAB_PASSWORD, or the `dashboard:`
credentials from the tenant's config/tenants YAML (demo tenants only).
Exits 0 when every run passed, 1 when any failed, 2 when it couldn't run
(e.g. the API has no ANTHROPIC_API_KEY).
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
import yaml

from app.modules.testlab.scenarios import Scenario, Turn, evaluate, load

CONFIG = Path(__file__).resolve().parents[3] / "config"


def _credentials(slug: str) -> tuple[str, str]:
    email, password = os.environ.get("LAB_EMAIL"), os.environ.get("LAB_PASSWORD")
    if email and password:
        return email, password
    for path in (CONFIG / "tenants").glob("*.yaml"):
        doc = yaml.safe_load(path.read_text())
        if doc["tenant"]["slug"] == slug and doc.get("dashboard"):
            return doc["dashboard"]["owner_email"], doc["dashboard"]["owner_password"]
    sys.exit(f"no credentials for {slug!r}: set LAB_EMAIL and LAB_PASSWORD")


def _play(client: httpx.Client, scenario: Scenario) -> list[Turn]:
    session = client.post("/test-lab/sessions", json={}).raise_for_status().json()
    sid = session["session_id"]
    turns: list[Turn] = []
    try:
        for line in scenario.turns:
            out = (
                client.post(f"/test-lab/sessions/{sid}/messages", json={"text": line}, timeout=120)
                .raise_for_status()
                .json()
            )
            turns.append(Turn(said=line, reply=out["reply"], tool_events=out["tool_events"]))
    finally:
        client.delete(f"/test-lab/sessions/{sid}")  # removes what the call created
    return turns


def _transcript(turns: list[Turn]) -> str:
    lines = []
    for t in turns:
        lines.append(f"      caller: {t.said}")
        for e in t.tool_events:
            lines.append(f"      [{e['tool']}] {e.get('arguments', {})} -> {e['result'][:120]}")
        lines.append(f"      agent:  {t.reply}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenarios", help="name in config/scenarios (e.g. example-auto) or a path")
    parser.add_argument("--api", default=os.environ.get("LAB_API", "http://localhost:8000"))
    parser.add_argument("--repeat", type=int, default=1, help="runs per scenario")
    parser.add_argument("--only", help="run scenarios whose name contains this")
    args = parser.parse_args()

    path = Path(args.scenarios)
    if not path.exists():
        path = CONFIG / "scenarios" / f"{args.scenarios}.yaml"
    slug, scenarios = load(path)
    if args.only:
        scenarios = [s for s in scenarios if args.only.lower() in s.name.lower()]

    client = httpx.Client(base_url=f"{args.api.rstrip('/')}/api/v1", timeout=30)
    email, password = _credentials(slug)
    try:
        login = client.post(
            "/auth/login", json={"tenant_slug": slug, "email": email, "password": password}
        )
    except httpx.TransportError:
        print(f"couldn't reach the API at {args.api}: is it running? (make dev)", file=sys.stderr)
        return 2
    if login.status_code != 200:
        print(f"couldn't sign in to {slug}: HTTP {login.status_code}", file=sys.stderr)
        return 2
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    if not client.get("/test-lab/status").raise_for_status().json()["llm_available"]:
        print(
            "The API has no ANTHROPIC_API_KEY, so simulated calls can't run. Add it to the "
            "API's environment and restart it.",
            file=sys.stderr,
        )
        return 2

    print(f"{len(scenarios)} scenarios x {args.repeat} against {slug} at {args.api}\n")
    failures: list[tuple[Scenario, int, list[str], list[Turn]]] = []
    rows = []
    for scenario in scenarios:
        passed = 0
        for run in range(1, args.repeat + 1):
            turns = _play(client, scenario)
            problems = evaluate(scenario, turns)
            if problems:
                failures.append((scenario, run, problems, turns))
            else:
                passed += 1
        rows.append((scenario.name, passed, args.repeat))
        mark = "PASS" if passed == args.repeat else "FAIL"
        print(f"  {mark}  {passed}/{args.repeat}  {scenario.name}", flush=True)

    total = sum(r[1] for r in rows)
    runs = sum(r[2] for r in rows)
    print(f"\n{total}/{runs} runs passed")
    for scenario, run, problems, turns in failures:
        print(f"\n  FAILED {scenario.name} (run {run})")
        for p in problems:
            print(f"    - {p}")
        print(_transcript(turns))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
