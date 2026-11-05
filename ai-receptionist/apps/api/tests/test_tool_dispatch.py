"""Unit tests for ToolExecutor dispatch behavior that don't need the real
database — unknown tools and failure handling. Full record/answer_faq/
callback flows are covered by the DB-backed integration tests."""

import uuid

from app.modules.conversation.tools import ToolExecutor
from app.modules.tenants.models import AgentConfig, Tenant
from tests.fakes import FakeEmbeddingProvider, FakeLLMProvider


def _tenant() -> Tenant:
    return Tenant(
        id=uuid.uuid4(),
        slug="test-practice",
        name="Test Practice",
        vertical="dental",
        timezone="America/New_York",
        business_hours={},
        settings={},
        status="active",
    )


def _agent_config() -> AgentConfig:
    return AgentConfig(
        tenant_id=uuid.uuid4(),
        version=1,
        is_active=True,
        system_prompt="You are a receptionist.",
        first_message="Hello!",
        qualification=[],
        escalation_policy={"callback_promise": "We'll call you back."},
    )


async def test_unknown_tool_name_returns_safe_fallback() -> None:
    executor = ToolExecutor(FakeLLMProvider(), FakeEmbeddingProvider())
    result = await executor.dispatch(
        tenant=_tenant(),
        call_id=uuid.uuid4(),
        caller_e164="+15550001111",
        agent_config=_agent_config(),
        tool_name="book_flight",
        arguments={},
    )
    assert "not able to do that" in result.lower()


async def test_dispatch_exception_returns_safe_fallback_not_raise() -> None:
    """A tool handler raising must never propagate — the webhook response
    still has to reach Vapi so the call doesn't go silent."""

    class ExplodingEmbeddingProvider(FakeEmbeddingProvider):
        async def embed(self, texts, *, for_query=False):
            raise RuntimeError("simulated embedding outage")

    executor = ToolExecutor(FakeLLMProvider(), ExplodingEmbeddingProvider())
    result = await executor.dispatch(
        tenant=_tenant(),
        call_id=uuid.uuid4(),
        caller_e164="+15550001111",
        agent_config=_agent_config(),
        tool_name="answer_faq",
        arguments={"question": "What are your hours?"},
    )
    assert "trouble" in result.lower()
