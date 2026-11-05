import json
from pathlib import Path

import yaml

from app.modules.conversation.qualification import score_answer

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "tenants"


def _as_stored(question: dict) -> dict:
    """`qualification` is written straight from the parsed YAML into a JSONB
    column (`seed_demo.py`) and read back through it on every call — never
    used in-memory as raw YAML. That round-trip matters here: PyYAML parses
    a bare `true:` as the Python bool `True`, but `json.dumps` coerces a
    bool dict key to the *string* `"true"` on the way into JSONB, which is
    the string `score_answer` looks up. Skipping this round-trip in a test
    would check a shape the code never actually sees."""
    return json.loads(json.dumps(question))


def test_boolean_true_scores() -> None:
    config = {"scoring": {"true": 30, "false": 0}}
    assert score_answer(config, True) == 30
    assert score_answer(config, False) == 0


def test_choice_scoring_matches_lowercased_key() -> None:
    config = {"scoring": {"has_deadline": 30}}
    assert score_answer(config, "has_deadline") == 30
    assert score_answer(config, "HAS_DEADLINE") == 30


def test_unmatched_answer_scores_zero() -> None:
    config = {"scoring": {"true": 30}}
    assert score_answer(config, "something else") == 0


def test_no_scoring_config_scores_zero() -> None:
    assert score_answer({}, True) == 0
    assert score_answer({"question": "What happened?"}, "a long free-text answer") == 0


def test_text_type_scoring_keyed_on_the_literal_answer_can_never_match_speech() -> None:
    """Documents the trap `example-legal.yaml`'s `urgency` question fell
    into: a `type: text` question's scoring key is matched against the
    caller's own words, so a key like `has_deadline` — which no caller says
    verbatim — silently never scores. `score_answer` isn't buggy here; a
    literal-key match is exactly right for `choice` questions (the options
    *are* the controlled vocabulary a caller's answer maps to). It's simply
    the wrong tool for open-ended `text` questions, which is why the fix
    was retyping the question to `boolean`, not changing this function."""
    assert score_answer({"scoring": {"has_deadline": 30}}, "yes, on the 15th") == 0


def test_legal_and_dental_configs_use_boolean_scoring_for_urgency_questions() -> None:
    """Regression test for the bug above: both tenant configs' urgency-style
    question (dental's `pain`, legal's `urgency`) must be `type: boolean`
    with a scoring key of `true`/`false` — the only question shape
    `score_answer` can actually score from natural spoken answers."""
    for filename, key in (("example-dental.yaml", "pain"), ("example-legal.yaml", "urgency")):
        doc = yaml.safe_load((CONFIG_DIR / filename).read_text())
        question = _as_stored(next(q for q in doc["qualification"] if q["key"] == key))
        assert question["type"] == "boolean", (
            f"{filename}:{key} is not `boolean` — its scoring key will never "
            "match a caller's actual words (see the test above)"
        )
        assert score_answer(question, True) > 0
        assert score_answer(question, False) == 0
