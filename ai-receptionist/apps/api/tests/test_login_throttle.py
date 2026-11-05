"""Login throttling: brute-force protection that is not itself an oracle.

The properties worth proving are the ones an attacker (or an unlucky user)
would actually hit: a lockout engages, it engages for accounts that don't
exist exactly as for ones that do, the right password gets no special
treatment while locked, parallel guesses are counted, and a good sign-in
clears the slate.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text

import app.modules.tenants.login as login_module
from app.core.config import Settings
from app.core.ratelimit import hit, refund
from app.main import create_app
from tests.conftest import requires_services
from tests.test_dashboard_auth_routes import _seed_tenant_with_users, _sync_engine

OWNER = "owner@test.example"


def _settings(**overrides) -> Settings:
    base = {"login_max_attempts_per_account": 3, "login_max_attempts_per_ip": 1000}
    return Settings(app_env="test", **{**base, **overrides})


def _post(client: TestClient, slug: str, email: str, password: str):
    return client.post(
        "/api/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": password}
    )


@pytest.fixture
def tenant():
    engine = _sync_engine()
    slug = f"throttle-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_tenant_with_users(engine, slug=slug)
    try:
        yield slug
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": str(tenant_id)})
        engine.dispose()


@requires_services
def test_account_locks_after_too_many_failures_with_retry_after(tenant) -> None:
    with TestClient(create_app(_settings())) as client:
        statuses = [_post(client, tenant, OWNER, "wrong").status_code for _ in range(3)]
        assert statuses == [401, 401, 401]

        blocked = _post(client, tenant, OWNER, "wrong")
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "too_many_attempts"
        assert 0 < int(blocked.headers["Retry-After"]) <= 900


@requires_services
def test_the_correct_password_gets_no_special_treatment_while_locked(tenant) -> None:
    """If a locked account still answered the right password differently, the
    lockout would double as a password oracle."""
    with TestClient(create_app(_settings())) as client:
        for _ in range(3):
            _post(client, tenant, OWNER, "wrong")
        assert _post(client, tenant, OWNER, "adminpass").status_code == 429


@requires_services
def test_unknown_accounts_lock_out_exactly_like_real_ones(tenant) -> None:
    with TestClient(create_app(_settings())) as client:
        for _ in range(3):
            _post(client, tenant, OWNER, "wrong")
        real = _post(client, tenant, OWNER, "wrong")

        ghost_slug = f"no-such-{uuid.uuid4().hex[:6]}"
        for _ in range(3):
            assert _post(client, ghost_slug, "ghost@test.example", "x").status_code == 401
        ghost = _post(client, ghost_slug, "ghost@test.example", "x")

        assert real.status_code == ghost.status_code == 429
        assert real.json() == ghost.json()


@requires_services
def test_a_successful_sign_in_clears_the_failure_count(tenant) -> None:
    with TestClient(create_app(_settings())) as client:
        assert [_post(client, tenant, OWNER, "wrong").status_code for _ in range(2)] == [401, 401]
        assert _post(client, tenant, OWNER, "adminpass").status_code == 200
        # Two more failures are fine again; without the reset the 3rd attempt
        # since the first failure would have been refused.
        assert [_post(client, tenant, OWNER, "wrong").status_code for _ in range(2)] == [401, 401]


@requires_services
def test_one_clients_failures_across_accounts_hit_the_ip_limit(tenant) -> None:
    with TestClient(create_app(_settings(login_max_attempts_per_ip=3))) as client:
        for i in range(3):
            assert _post(client, tenant, f"user{i}@test.example", "x").status_code == 401
        assert _post(client, tenant, "user9@test.example", "x").status_code == 429


@requires_services
def test_successes_do_not_consume_the_ip_budget(tenant) -> None:
    """A whole office behind one address signs in all morning."""
    with TestClient(create_app(_settings(login_max_attempts_per_ip=3))) as client:
        for _ in range(6):
            assert _post(client, tenant, OWNER, "adminpass").status_code == 200


@requires_services
def test_a_parallel_burst_cannot_slip_extra_guesses_past_the_limit(tenant) -> None:
    with (
        TestClient(create_app(_settings())) as client,
        ThreadPoolExecutor(max_workers=12) as pool,
    ):
        responses = list(pool.map(lambda _: _post(client, tenant, OWNER, "wrong"), range(12)))
    statuses = [r.status_code for r in responses]
    assert statuses.count(401) <= 3, statuses
    assert statuses.count(429) >= 9, statuses


@requires_services
def test_login_still_works_when_redis_is_unavailable(tenant) -> None:
    async def down(*args, **kwargs):
        raise RedisConnectionError("simulated outage")

    with TestClient(create_app(_settings())) as client, patch.object(login_module, "hit", down):
        assert _post(client, tenant, OWNER, "adminpass").status_code == 200


@requires_services
def test_unknown_accounts_cost_the_same_bcrypt_work_as_wrong_passwords(tenant) -> None:
    """Response time must not reveal which accounts exist: every failure path,
    including the ones that never reach a real hash, runs one bcrypt check."""
    calls: list[str] = []
    real_verify = login_module.verify_password

    def counting_verify(password: str, hashed: str) -> bool:
        calls.append(hashed)
        return real_verify(password, hashed)

    with (
        TestClient(create_app(_settings(login_max_attempts_per_account=50))) as client,
        patch.object(login_module, "verify_password", counting_verify),
    ):
        _post(client, tenant, OWNER, "wrong")  # real user, wrong password
        _post(client, tenant, "ghost@test.example", "wrong")  # unknown email
        _post(client, f"no-such-{uuid.uuid4().hex[:6]}", OWNER, "wrong")  # unknown business
    assert len(calls) == 3


# ── the counter itself ───────────────────────────────────────────────────


@requires_services
async def test_counter_counts_and_always_expires() -> None:
    import redis.asyncio as aioredis

    redis = aioredis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    key = f"rl:test:{uuid.uuid4().hex}"
    try:
        assert (await hit(redis, key, 60))[0] == 1
        count, ttl = await hit(redis, key, 60)
        assert count == 2
        assert 0 < ttl <= 60
    finally:
        await redis.delete(key)
        await redis.aclose()


@requires_services
async def test_a_counter_missing_its_expiry_is_repaired_not_left_permanent() -> None:
    """The failure mode this guards against is a login lockout that never
    lifts because a key lost its TTL."""
    import redis.asyncio as aioredis

    redis = aioredis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    key = f"rl:test:{uuid.uuid4().hex}"
    try:
        await redis.set(key, 5)  # no TTL
        assert await redis.ttl(key) == -1
        count, ttl = await hit(redis, key, 60)
        assert count == 6
        assert 0 < ttl <= 60
        assert 0 < await redis.ttl(key) <= 60
    finally:
        await redis.delete(key)
        await redis.aclose()


@requires_services
async def test_refund_never_goes_below_zero() -> None:
    import redis.asyncio as aioredis

    redis = aioredis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    key = f"rl:test:{uuid.uuid4().hex}"
    try:
        await refund(redis, key)
        await hit(redis, key, 60)
        await refund(redis, key)
        await refund(redis, key)
        assert int(await redis.get(key) or 0) == 0
    finally:
        await redis.delete(key)
        await redis.aclose()
