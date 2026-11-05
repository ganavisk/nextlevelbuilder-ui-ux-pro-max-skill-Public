"""Integration test: a TOOL_CALL webhook driven through the real HTTP route,
against live Postgres + Redis, with fake LLM/embedding providers swapped in
after app startup (see main.py's lifespan comment) so it needs no live
Anthropic/Voyage keys. This is the exact code path a live call takes:
parse -> resolve call context -> tenant's active agent config -> tool
dispatch -> Vapi-shaped response.
"""

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.main import create_app
from app.modules.conversation.faq import NO_ANSWER_FALLBACK
from tests.conftest import requires_services
from tests.fakes import FakeEmbeddingProvider, FakeLLMProvider

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-secret"


@requires_services
def test_answer_faq_tool_call_grounds_on_seeded_knowledge() -> None:
    settings = Settings(_env_file=None, app_env="test", vapi_webhook_secret=SECRET)
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)

    tenant_id = uuid.uuid4()
    number = f"+1998{uuid.uuid4().int % 10**7:07d}"
    vendor_call_id = f"toolcall-itest-{uuid.uuid4().hex[:10]}"

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, settings) "
                "VALUES (:id, :slug, 'Tool Call Test', 'dental', "
                '\'{"forbidden_topics": ["clinical advice"]}\')'
            ),
            {"id": str(tenant_id), "slug": f"toolcall-test-{tenant_id.hex[:6]}"},
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text("INSERT INTO phone_numbers (tenant_id, e164) VALUES (:tid, :e164)"),
            {"tid": str(tenant_id), "e164": number},
        )
        conn.execute(
            text(
                "INSERT INTO agent_configs "
                "(tenant_id, version, is_active, system_prompt, first_message, "
                "escalation_policy) VALUES "
                "(:tid, 1, true, 'You are a receptionist.', 'Hello!', "
                '\'{"callback_promise": "We will call you back."}\')'
            ),
            {"tid": str(tenant_id)},
        )
        # A TOOL_CALL webhook always follows call_started in a real call;
        # resolve_call_context's DB fallback (no Redis state here) needs an
        # existing calls row for this vendor_call_id to resolve against.
        conn.execute(
            text(
                "INSERT INTO calls (tenant_id, vendor_call_id, caller_e164) "
                "VALUES (:tid, :vcid, '+15559998888')"
            ),
            {"tid": str(tenant_id), "vcid": vendor_call_id},
        )

    try:
        app = create_app(settings)
        with TestClient(app) as client:
            # Swap in fakes post-startup (lifespan already ran and set the
            # real providers — see main.py's comment on why this ordering
            # matters).
            fake_llm = FakeLLMProvider(response_text="We are open 9 to 5, Monday to Friday.")
            app.state.llm_provider = fake_llm
            app.state.embedding_provider = FakeEmbeddingProvider()

            body = json.dumps(
                {
                    "message": {
                        "type": "tool-calls",
                        "toolCallList": [
                            {
                                "id": "toolcall-abc",
                                "name": "answer_faq",
                                "arguments": {"question": "What are your hours?"},
                            },
                            {
                                "id": "toolcall-def",
                                "name": "request_callback",
                                "arguments": {"reason": "wants a human"},
                            },
                        ],
                        "call": {"id": vendor_call_id, "customer": {"number": "+15559998888"}},
                        "phoneNumber": {"number": number},
                    }
                }
            )
            response = client.post(
                "/webhooks/voice/vapi", content=body, headers={"x-vapi-secret": SECRET}
            )

        assert response.status_code == 200
        results = {r["toolCallId"]: r["result"] for r in response.json()["results"]}
        # No knowledge chunks were seeded -> FAQ falls back safely rather
        # than fabricating an answer. Retrieval short-circuits before the
        # LLM is even called (fake_llm.calls stays empty), which is the
        # real hallucination guardrail — not just the wording of the reply.
        assert results["toolcall-abc"] == NO_ANSWER_FALLBACK
        assert fake_llm.calls == []
        assert results["toolcall-def"] == "We will call you back."

        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
            )
            callback = conn.execute(
                text("SELECT reason, caller_e164 FROM callback_requests WHERE tenant_id = :tid"),
                {"tid": str(tenant_id)},
            ).one()
            assert callback.reason == "wants a human"
            assert callback.caller_e164 == "+15559998888"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()
