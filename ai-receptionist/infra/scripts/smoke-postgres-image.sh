#!/usr/bin/env bash
# Smoke-test the Postgres image (infra/postgres/image) the way a deploy
# exercises it:
#
#   infra/scripts/smoke-postgres-image.sh receptionist-postgres:ci
#
# Checks:
# - it refuses to start without backup settings;
# - with BACKUP_MODE=off and passwords, it initializes, becomes healthy, and
#   has the extensions, data checksums and a restricted app role;
# - Postgres runs as postgres, not root, and logs no FATAL or ERROR;
# - a stop is a clean fast shutdown, and a restart reuses the data.
#
# Backups to a bucket aren't exercised here; that is the restore drill in
# docs/runbooks/postgres-image.md.
#
# With KEEP=1 the database is left running on SMOKE_PG_PORT (default 5432)
# for a test run, with APP_DB_PASSWORD (default ci-only-password).
set -euo pipefail

IMAGE="${1:?usage: smoke-postgres-image.sh IMAGE}"
PORT="${SMOKE_PG_PORT:-5432}"
APP_DB_PASSWORD="${APP_DB_PASSWORD:-ci-only-password}"
NAME=smoke-postgres

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; docker logs "$NAME" 2>&1 | tail -20 >&2 || true; exit 1; }
cleanup() { [[ ${KEEP:-0} == 1 ]] || docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker rm -f "$NAME" >/dev/null 2>&1 || true

sql() { docker exec -u postgres "$NAME" psql -XAtq -p 5432 -d receptionist -c "$1"; }
healthy() {
  for _ in $(seq 1 60); do
    [[ $(docker inspect --format '{{.State.Health.Status}}' "$NAME") == healthy ]] && return 0
    sleep 2
  done
  return 1
}

echo "postgres image: $IMAGE"

out=$(timeout 60 docker run --rm "$IMAGE" 2>&1) && fail "started without backup settings"
grep -q "backups are required" <<<"$out" || fail "no clear message without backup settings: $out"
pass "refuses to start without backup settings"

docker run -d --name "$NAME" -p "$PORT:5432" -e BACKUP_MODE=off \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" -e APP_DB_PASSWORD="$APP_DB_PASSWORD" \
  "$IMAGE" >/dev/null
healthy || fail "never became healthy"
pass "initializes and becomes healthy"

ext=$(sql "SELECT string_agg(extname || ' ' || extversion, ', ' ORDER BY extname) FROM pg_extension WHERE extname IN ('vector','btree_gist','pgcrypto')")
[[ $(grep -o ',' <<<"$ext" | wc -l) == 2 ]] || fail "extensions: $ext"
pass "extensions: $ext"
[[ $(sql "SHOW data_checksums") == on ]] || fail "data checksums off"
pass "data checksums on"
[[ $(sql "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'receptionist_app'") == f ]] \
  || fail "receptionist_app missing, or can bypass RLS"
pass "receptionist_app exists and can't bypass RLS"

owners=$(docker exec "$NAME" sh -c 'for p in /proc/[0-9]*; do [ "$(cat $p/comm 2>/dev/null)" = postgres ] && stat -c %U $p; done | sort -u')
[[ $owners == postgres ]] || fail "postgres processes owned by: $owners"
pass "postgres runs as postgres"

start=$(date +%s)
docker stop "$NAME" >/dev/null
(( $(date +%s) - start <= 15 )) || fail "stop took over 15 s"
[[ $(docker inspect --format '{{.State.ExitCode}}' "$NAME") == 0 ]] || fail "non-zero exit on stop"
docker logs "$NAME" 2>&1 | grep -q "database system is shut down" || fail "no clean shutdown"
pass "stops cleanly (fast shutdown, exit 0)"

docker start "$NAME" >/dev/null
healthy || fail "not healthy after restart"
[[ $(docker logs "$NAME" 2>&1 | grep -c "initializing a new database") == 1 ]] || fail "re-initialized on restart"
pass "restarts on the same data"

if docker logs "$NAME" 2>&1 | grep -E " (FATAL|ERROR|PANIC): "; then fail "errors in the log"; fi
pass "no FATAL or ERROR in the log"

[[ ${KEEP:-0} == 1 ]] && echo "left running on port $PORT (receptionist_app / APP_DB_PASSWORD)"
echo "postgres image smoke test passed"
