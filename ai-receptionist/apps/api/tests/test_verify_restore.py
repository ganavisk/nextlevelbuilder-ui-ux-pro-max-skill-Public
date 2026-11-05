"""scripts/verify_restore.py decides whether a restored database may serve.
A checker that can't fail proves nothing, so these tests make it fail."""

import copy
import importlib.util
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import Settings
from tests.conftest import requires_services

API = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "verify_restore", API / "scripts" / "verify_restore.py"
)
verify_restore = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(verify_restore)  # type: ignore[union-attr]

T1, T2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
SNAPSHOT: dict[str, Any] = {
    "revision": "0012",
    "taken_at": "2026-11-04 18:31:15+00",
    "tables": {
        "tenants": {T1: [1, "a"], T2: [1, "b"]},
        "appointments": {T1: [36, "c"], T2: [5, "d"]},
        "calls": {T1: [200, "e"], T2: [50, "f"]},
    },
}


def _failed(results: list[tuple[str, bool, str]]) -> dict[str, str]:
    return {name: detail for name, ok, detail in results if not ok}


def test_an_identical_restore_passes() -> None:
    assert _failed(verify_restore.compare(SNAPSHOT, copy.deepcopy(SNAPSHOT))) == {}


def test_a_lost_tenant_fails_every_table_it_had_rows_in_and_is_named() -> None:
    restored = copy.deepcopy(SNAPSHOT)
    for rows in restored["tables"].values():
        rows.pop(T1)
    failed = _failed(verify_restore.compare(SNAPSHOT, restored))
    assert set(failed) == {"tenants identical", "appointments identical", "calls identical"}
    assert all(T1 in detail for detail in failed.values())


def test_same_row_count_but_different_rows_fails() -> None:
    """Counts alone would pass a restore that holds the wrong rows."""
    restored = copy.deepcopy(SNAPSHOT)
    restored["tables"]["appointments"][T2] = [5, "other-content"]
    assert set(_failed(verify_restore.compare(SNAPSHOT, restored))) == {"appointments identical"}


def test_rows_the_snapshot_never_had_fail_too() -> None:
    """A restore past the target (the accident replayed) must not pass."""
    restored = copy.deepcopy(SNAPSHOT)
    restored["tables"]["calls"]["33333333-3333-3333-3333-333333333333"] = [1, "g"]
    assert set(_failed(verify_restore.compare(SNAPSHOT, restored))) == {"calls identical"}


def test_a_different_schema_revision_fails() -> None:
    restored = copy.deepcopy(SNAPSHOT)
    restored["revision"] = "0011"
    assert "same schema revision as the snapshot" in _failed(
        verify_restore.compare(SNAPSHOT, restored)
    )


@requires_services
def test_the_live_schema_passes_every_structural_check() -> None:
    """The test database is migrated to head with the real policies, so every
    structural check must pass on it. If this fails, either the schema or
    the checker is wrong, and a restore drill would report nonsense."""
    engine = verify_restore._engine(Settings(_env_file=None, app_env="test").database_url)
    with engine.connect() as conn:
        role = conn.execute(text("SELECT current_user")).scalar_one()
        results = verify_restore.structural_checks(conn, role)
        snapshot_twice = [verify_restore.snapshot(conn) for _ in range(2)]
    engine.dispose()
    assert _failed(results) == {}
    assert len(results) == 6
    # Deterministic: the same database snapshots identically.
    assert _failed(verify_restore.compare(*snapshot_twice)) == {}


@requires_services
def test_a_missing_role_fails_the_role_check() -> None:
    engine = verify_restore._engine(Settings(_env_file=None, app_env="test").database_url)
    with engine.connect() as conn:
        failed = _failed(verify_restore.structural_checks(conn, "no_such_role"))
    engine.dispose()
    assert failed == {"no_such_role exists, can log in, can't bypass RLS": "role missing"}
