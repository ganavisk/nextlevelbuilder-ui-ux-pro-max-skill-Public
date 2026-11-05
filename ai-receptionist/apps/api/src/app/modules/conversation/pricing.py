"""LLM cost estimation for per-tenant metering (Phase 1 NFR-09).

Rates are Anthropic's per-million-token API pricing for the models this
platform is pinned to (see docs/ai-receptionist for the model catalog);
revisit if pricing or the pinned models change.
"""

_RATES_PER_MTOK_USD: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost_cents(model: str, input_tokens: int, output_tokens: int) -> int:
    """Unrecognized model ids (e.g. a dated snapshot the SDK expanded the
    alias to) price at zero rather than raising — cost metering should never
    be why an in-call tool response fails."""
    input_rate, output_rate = _RATES_PER_MTOK_USD.get(model, (0.0, 0.0))
    dollars = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return round(dollars * 100)
