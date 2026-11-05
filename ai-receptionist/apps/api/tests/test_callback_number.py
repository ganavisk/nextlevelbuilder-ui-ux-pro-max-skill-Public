"""request_callback takes the number the caller wants to be called back on.

A callback is a promise: the caller hangs up expecting the phone to ring. So
a number they gave replaces the caller ID for the team to dial, a misheard
number saves nothing and is asked for again, and a blocked caller ID with no
number given is asked for one rather than queued as a callback nobody can
return.
"""

import uuid

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.phone import NUMBER_UNCLEAR
from app.modules.conversation.tools import CALLBACK_NUMBER_NEEDED, ToolExecutor
from app.modules.tenants.models import Tenant
from tests.conftest import requires_services
from tests.fakes import FakeEmbeddingProvider, FakeLLMProvider
from tests.test_tool_dispatch import _agent_config

PROMISE = "We'll call you back."


def _engine():
    url = Settings(_env_file=None, app_env="test").database_url
    return create_engine(url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1))


@pytest.fixture
def tenant():
    tenant_id = uuid.uuid4()
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical) "
                "VALUES (:id, :slug, 'Callback Garage', 'auto_repair')"
            ),
            {"id": str(tenant_id), "slug": f"cb-{tenant_id.hex[:8]}"},
        )
    try:
        yield Tenant(
            id=tenant_id,
            slug=f"cb-{tenant_id.hex[:8]}",
            name="Callback Garage",
            vertical="auto_repair",
            timezone="America/Denver",
            business_hours={},
            settings={},
            status="active",
        )
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


async def _callback(tenant: Tenant, *, caller_id: str, **arguments) -> str:
    executor = ToolExecutor(FakeLLMProvider(), FakeEmbeddingProvider())
    return await executor.dispatch(
        tenant=tenant,
        call_id=None,
        caller_e164=caller_id,
        agent_config=_agent_config(),
        tool_name="request_callback",
        arguments={"reason": "Wants a technician to call about a noise", **arguments},
    )


def _saved(tenant: Tenant) -> list[tuple[str, str | None]]:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant.id)})
        rows = conn.execute(
            text("SELECT caller_e164, callback_e164 FROM callback_requests WHERE tenant_id = :t"),
            {"t": str(tenant.id)},
        ).all()
    engine.dispose()
    return [(r[0], r[1]) for r in rows]


@requires_services
async def test_a_different_number_is_saved_for_the_team_and_read_back(tenant) -> None:
    spoken = await _callback(tenant, caller_id="+15550009999", customer_phone="720-555-0142")
    assert spoken == f"{PROMISE} I have your number as 7 2 0, 5 5 5, 0 1 4 2."
    # The caller ID is kept (it's who called); the team dials the new number.
    assert _saved(tenant) == [("+15550009999", "+17205550142")]


@requires_services
async def test_without_a_number_the_caller_id_is_used_as_before(tenant) -> None:
    assert await _callback(tenant, caller_id="+15550009999") == PROMISE
    assert _saved(tenant) == [("+15550009999", None)]


@requires_services
async def test_giving_the_number_they_called_from_changes_nothing(tenant) -> None:
    spoken = await _callback(tenant, caller_id="+17205550142", customer_phone="(720) 555-0142")
    assert spoken == PROMISE  # nothing new to read back
    assert _saved(tenant) == [("+17205550142", None)]


@requires_services
async def test_a_misheard_number_saves_nothing_and_asks_again(tenant) -> None:
    assert await _callback(tenant, caller_id="+15550009999", customer_phone="720-555-014") == (
        NUMBER_UNCLEAR
    )
    assert _saved(tenant) == []


@requires_services
async def test_a_blocked_caller_id_is_asked_for_a_number_first(tenant) -> None:
    assert await _callback(tenant, caller_id="") == CALLBACK_NUMBER_NEEDED
    assert _saved(tenant) == []  # no callback nobody could return
    spoken = await _callback(tenant, caller_id="", customer_phone="720-555-0142")
    assert spoken.startswith(PROMISE)
    assert _saved(tenant) == [("", "+17205550142")]


def test_the_prompt_no_longer_asks_every_caller_for_their_number() -> None:
    """Asking for a number the caller ID already gives is a wasted turn."""
    from app.modules.tenants.service import assemble_system_prompt
    from tests.test_tool_dispatch import _tenant

    prompt = assemble_system_prompt(_tenant(), _agent_config())
    assert "collect their name, number, and reason" not in prompt
    assert "a number only if they want a call back somewhere other" in prompt


# ── who to ask for ──────────────────────────────────────────────────────


def _names(tenant: Tenant) -> list[tuple[str, str]]:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant.id)})
        rows = conn.execute(
            text("SELECT caller_name, reason FROM callback_requests WHERE tenant_id = :t"),
            {"t": str(tenant.id)},
        ).all()
    engine.dispose()
    return [(r[0], r[1]) for r in rows]


@requires_services
async def test_the_callers_name_is_saved_apart_from_the_reason(tenant) -> None:
    await _callback(
        tenant,
        caller_id="+15550009999",
        customer_name="Kenji Watanabe",
        reason="Grinding noise from the front brakes",
    )
    assert _names(tenant) == [("Kenji Watanabe", "Grinding noise from the front brakes")]


@requires_services
async def test_a_caller_who_wont_give_a_name_still_gets_a_callback(tenant) -> None:
    """A name is useful; a callback is the promise. Never refuse one for it."""
    assert await _callback(tenant, caller_id="+15550009999") == PROMISE
    assert _names(tenant)[0][0] == ""


@requires_services
async def test_a_name_is_tidied_and_capped_before_it_is_stored(tenant) -> None:
    await _callback(tenant, caller_id="+15550009999", customer_name="  Ana   María\nLima ")
    await _callback(tenant, caller_id="+15550008888", customer_name="x" * 500)
    stored = sorted(name for name, _ in _names(tenant))
    assert "Ana María Lima" in stored
    assert max(len(n) for n in stored) == 120


def test_the_tool_asks_for_the_name_in_its_own_field() -> None:
    from app.modules.conversation.tools import TOOL_SCHEMAS

    callback = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "request_callback")
    assert "customer_name" in callback["function"]["parameters"]["properties"]
    assert "customer_name" not in callback["function"]["parameters"]["required"]
    assert "not in the reason" in callback["function"]["description"]
