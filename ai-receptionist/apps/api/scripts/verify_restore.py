"""Prove a restored database is complete and safe to serve.

A backup is only as good as its last tested restore. This script makes the
test objective. Run `snapshot` against the source at (or just before) the
recovery target, restore, then `verify` the copy against that snapshot:

    python scripts/verify_restore.py snapshot --url "$ADMIN_DATABASE_URL" > at-target.json
    # ...restore to the target time...
    python scripts/verify_restore.py verify --url "$RESTORED_ADMIN_URL" \
        --against at-target.json --app-url "$RESTORED_APP_URL"

`verify` without --against still checks the structure. That is the check to
run after a real disaster, when there is no snapshot to compare with.

Checks:
- the schema is at the migrations' head revision;
- the extensions the schema relies on (vector, btree_gist) are present;
- every table with a tenant_id has row-level security enabled and forced,
  with its tenant_isolation policy (found from the catalog, so a new table
  can't be missed);
- the application role (--app-role) exists and can't bypass RLS;
- the per-bay booking exclusion constraint survived, and no index is
  INVALID;
- with --against: per tenant and per table, the same row count and the same
  content hash (every column of every row), so a restore that is short of
  rows, or holds different ones, fails;
- with --app-url: the application role can read a tenant's rows with the
  tenant set, and sees nothing without it.

--url must be an admin login (superuser or BYPASSRLS). RLS is forced, so
the application role sees no rows without a tenant and couldn't count them.
Exit status 0 means every check passed; 1 means at least one failed.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

API = Path(__file__).resolve().parents[1]
EXTENSIONS = ("vector", "btree_gist")


def _engine(url: str):  # type: ignore[no-untyped-def]
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix) :]
            break
    return create_engine(url)


def tenant_tables(conn: Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' "
                "AND NOT a.attisdropped "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1"
            )
        )
    ]


def snapshot(conn: Connection) -> dict[str, Any]:
    """Row counts and content hashes, per tenant and table, plus the schema
    revision. Hashes cover whole rows in a canonical order, so they match
    only if the restored rows are the same rows."""
    tables: dict[str, dict[str, list[Any]]] = {}
    for table in ["tenants", *tenant_tables(conn)]:
        key = "id" if table == "tenants" else "tenant_id"
        rows = conn.execute(
            text(
                f"SELECT {key}::text, count(*), "
                f"md5(string_agg(t::text, E'\\n' ORDER BY t::text)) "
                f"FROM {table} t GROUP BY 1"
            )
        )
        tables[table] = {r[0]: [r[1], r[2]] for r in rows}
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    taken_at = conn.execute(text("SELECT now()::text")).scalar_one()
    return {"revision": revision, "taken_at": taken_at, "tables": tables}


def head_revision() -> str:
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def structural_checks(conn: Connection, app_role: str) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    head = head_revision()
    check("schema at head revision", revision == head, f"{revision} (head {head})")

    present = {r[0] for r in conn.execute(text("SELECT extname FROM pg_extension"))}
    missing = [e for e in EXTENSIONS if e not in present]
    check("extensions present", not missing, f"missing: {missing}" if missing else "")

    tables = tenant_tables(conn)
    unprotected = [
        r[0]
        for r in conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY(:t) "
                "AND NOT (c.relrowsecurity AND c.relforcerowsecurity AND EXISTS ("
                "  SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid "
                "  AND p.polname = 'tenant_isolation' "
                "  AND p.polqual IS NOT NULL AND p.polwithcheck IS NOT NULL))"
            ),
            {"t": tables},
        )
    ]
    check(
        f"row-level security forced on all {len(tables)} tenant tables",
        bool(tables) and not unprotected,
        f"unprotected: {unprotected}" if unprotected else "",
    )

    role = conn.execute(
        text("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r"),
        {"r": app_role},
    ).first()
    check(
        f"{app_role} exists, can log in, can't bypass RLS",
        role is not None and not role.rolsuper and not role.rolbypassrls and role.rolcanlogin,
        "role missing" if role is None else "",
    )

    exclusion = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'appointments'::regclass AND contype = 'x'"
        )
    ).scalar_one()
    check("booking exclusion constraint present", exclusion >= 1)

    invalid = [
        r[0]
        for r in conn.execute(
            text("SELECT indexrelid::regclass::text FROM pg_index WHERE NOT indisvalid")
        )
    ]
    check("no INVALID indexes", not invalid, ", ".join(invalid))
    return results


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[tuple[str, bool, str]]:
    results = [
        (
            "same schema revision as the snapshot",
            expected["revision"] == actual["revision"],
            f"{actual['revision']} vs {expected['revision']}",
        )
    ]
    for table, want in sorted(expected["tables"].items()):
        got = actual["tables"].get(table, {})
        wrong = sorted(k for k in want.keys() | got.keys() if want.get(k) != got.get(k))
        rows = sum(v[0] for v in want.values())
        detail = f"{rows} rows, {len(want)} tenants"
        if wrong:
            detail += f"; differs for {len(wrong)}: " + ", ".join(wrong[:3])
        results.append((f"{table} identical", not wrong, detail))
    return results


def app_role_checks(app_url: str, admin: Connection) -> list[tuple[str, bool, str]]:
    tenant = admin.execute(
        text("SELECT tenant_id::text FROM appointments GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
    ).scalar()
    if tenant is None:
        return [("application role reads with a tenant set", False, "no appointments")]
    expected = admin.execute(
        text("SELECT count(*) FROM appointments WHERE tenant_id = :t"), {"t": tenant}
    ).scalar_one()
    engine = _engine(app_url)
    with engine.begin() as conn:
        unset = conn.execute(text("SELECT count(*) FROM appointments")).scalar_one()
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        scoped = conn.execute(text("SELECT count(*) FROM appointments")).scalar_one()
    engine.dispose()
    return [
        ("application role sees nothing without a tenant", unset == 0, f"{unset} rows"),
        (
            "application role reads its tenant's rows",
            scoped == expected,
            f"{scoped} of {expected} appointments",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="print a JSON manifest of the database")
    snap.add_argument("--url", required=True, help="admin login (superuser or BYPASSRLS)")
    ver = sub.add_parser("verify", help="check a restored database")
    ver.add_argument("--url", required=True, help="admin login to the restored database")
    ver.add_argument("--against", type=Path, help="a snapshot taken at the recovery target")
    ver.add_argument("--app-url", help="the application role's login to the restored database")
    ver.add_argument("--app-role", default="receptionist_app", help="default: %(default)s")
    args = parser.parse_args()

    engine = _engine(args.url)
    with engine.connect() as conn:
        if args.command == "snapshot":
            json.dump(snapshot(conn), sys.stdout, indent=1, sort_keys=True)
            print()
            return 0
        results = structural_checks(conn, args.app_role)
        if args.against:
            results += compare(json.loads(args.against.read_text()), snapshot(conn))
        if args.app_url:
            results += app_role_checks(args.app_url, conn)
    engine.dispose()

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
