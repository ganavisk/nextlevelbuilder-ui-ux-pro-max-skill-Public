from app.modules.conversation.pricing import estimate_cost_cents


def test_known_model_cost() -> None:
    # 1M input tokens at $3/MTok = 300 cents; 1M output at $15/MTok = 1500 cents.
    assert estimate_cost_cents("claude-sonnet-5", 1_000_000, 0) == 300
    assert estimate_cost_cents("claude-sonnet-5", 0, 1_000_000) == 1500
    assert estimate_cost_cents("claude-haiku-4-5", 1_000_000, 1_000_000) == 600


def test_unknown_model_prices_at_zero() -> None:
    assert estimate_cost_cents("some-future-model", 1_000_000, 1_000_000) == 0


def test_zero_tokens_zero_cost() -> None:
    assert estimate_cost_cents("claude-sonnet-5", 0, 0) == 0
