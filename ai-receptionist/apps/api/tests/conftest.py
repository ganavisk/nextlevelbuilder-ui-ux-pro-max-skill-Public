import socket

import pytest
import pytest_asyncio

import app.models  # noqa: F401 — registers every ORM model on Base.metadata
from app.core import db as db_module


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


requires_services = pytest.mark.skipif(
    not (_reachable("localhost", 5432) and _reachable("localhost", 6379)),
    reason="requires local Postgres and Redis",
)


@pytest_asyncio.fixture(autouse=True)
async def _reset_db_engine():
    """pytest-asyncio gives each test function its own event loop by
    default, but core/db.py's engine is a lazily-created module-level
    singleton — reused across tests, it holds asyncpg connections bound to
    a now-dead loop and every DB call after the first test fails with
    "attached to a different loop". Dispose and drop it after every test so
    the next one that touches the database builds a fresh engine bound to
    its own loop."""
    yield
    if db_module._engine is not None:
        await db_module._engine.dispose()
        db_module._engine = None
        db_module._session_factory = None


@pytest.fixture(autouse=True)
def _clear_rate_limit_counters():
    """Login throttle counters live in Redis for 15 minutes and are keyed by
    client address — which is the same ("testclient") for every route test —
    so without this they would leak between tests and across suite runs."""
    if _reachable("localhost", 6379):
        import redis

        from app.core.config import Settings

        client = redis.Redis.from_url(Settings(_env_file=None).redis_url)
        keys = list(client.scan_iter("rl:*"))
        if keys:
            client.delete(*keys)
        client.close()
    yield
