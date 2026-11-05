from app.modules.conversation.summarization import summarize_call
from tests.fakes import FakeLLMProvider, RaisingLLMProvider


async def test_empty_transcript_returns_fallback_without_calling_llm() -> None:
    llm = FakeLLMProvider()
    result = await summarize_call([], llm)
    assert result.classification == {"summary": None, "outcome": "other", "sentiment": None}
    assert llm.calls == []


async def test_valid_json_response_parsed() -> None:
    llm = FakeLLMProvider(
        response_text=(
            '{"summary": "Caller asked about hours.", "outcome": "answered_faq", '
            '"sentiment": "positive"}'
        )
    )
    result = await summarize_call([{"role": "caller", "text": "What are your hours?"}], llm)
    assert result.classification == {
        "summary": "Caller asked about hours.",
        "outcome": "answered_faq",
        "sentiment": "positive",
    }
    assert result.input_tokens == 10
    assert result.model == "fake-fast"
    # summarization is a classification task — must use the fast model.
    assert llm.calls[0]  # sanity: a call was recorded


async def test_invalid_outcome_falls_back_to_other() -> None:
    llm = FakeLLMProvider(response_text='{"summary": "ok", "outcome": "not_a_real_outcome"}')
    result = await summarize_call([{"role": "caller", "text": "hi"}], llm)
    assert result.classification["outcome"] == "other"


async def test_non_json_response_degrades_safely() -> None:
    llm = FakeLLMProvider(response_text="I refuse to respond in JSON.")
    result = await summarize_call([{"role": "caller", "text": "hi"}], llm)
    assert result.classification == {"summary": None, "outcome": "other", "sentiment": None}


async def test_llm_failure_degrades_safely() -> None:
    result = await summarize_call([{"role": "caller", "text": "hi"}], RaisingLLMProvider())
    assert result.classification == {"summary": None, "outcome": "other", "sentiment": None}
    assert result.input_tokens == 0
