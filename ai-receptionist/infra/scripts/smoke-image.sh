#!/usr/bin/env bash
# Smoke-test the production image the way a deploy exercises it.
#
#   DATABASE_URL=postgresql+asyncpg://receptionist_app:...@localhost:5432/db \
#   REDIS_URL=redis://localhost:6379/0 \
#   infra/scripts/smoke-image.sh receptionist-api:ci
#
# DATABASE_URL must be the restricted application role (see
# infra/postgres/bootstrap-managed.sql) on a database that role can migrate:
# the API and workers run with APP_ENV=production here, so they enforce that.
# Uses host networking so it works the same on a CI runner and a laptop.

set -euo pipefail

IMAGE="${1:?usage: smoke-image.sh IMAGE}"
: "${DATABASE_URL:?set DATABASE_URL}"
: "${REDIS_URL:?set REDIS_URL}"
PORT="${SMOKE_PORT:-8123}"
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
NAMES=(smoke-api smoke-worker smoke-confirmations smoke-crm)

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; for n in "${NAMES[@]}"; do docker logs "$n" 2>&1 | tail -15 >&2 || true; done; exit 1; }
cleanup() { docker rm -f "${NAMES[@]}" smoke-weak >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

run_role() { # name role [extra docker args...]
  local name="$1" role="$2"; shift 2
  docker run -d --name "$name" --network host \
    -e APP_ENV=production -e SECRET_KEY="$SECRET_KEY" \
    -e DATABASE_URL="$DATABASE_URL" -e REDIS_URL="$REDIS_URL" -e PORT="$PORT" "$@" \
    "$IMAGE" "$role" >/dev/null
}

echo "image: $IMAGE"

echo "migrate"
docker run --rm --network host -e DATABASE_URL="$DATABASE_URL" "$IMAGE" migrate >/dev/null \
  && pass "alembic upgrade head exits 0" || fail "migrate"

echo "refuses unsafe configuration"
if docker run --name smoke-weak --network host -e APP_ENV=production -e SECRET_KEY=dev-only-secret \
     -e DATABASE_URL="$DATABASE_URL" -e REDIS_URL="$REDIS_URL" "$IMAGE" api >/dev/null 2>&1; then
  fail "booted in production with the default SECRET_KEY"
fi
docker logs smoke-weak 2>&1 | grep -q "SECRET_KEY must be set" \
  && pass "weak SECRET_KEY rejected in production" || fail "weak SECRET_KEY rejected for the wrong reason"

echo "api"
run_role smoke-api api
for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "localhost:$PORT/readyz")" = "200" ] && break; sleep 1
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "localhost:$PORT/readyz")" = "200" ] && pass "/readyz 200 (database + redis reachable)" || fail "/readyz"
[ "$(curl -s -o /dev/null -w '%{http_code}' "localhost:$PORT/docs")" = "404" ] && pass "/docs disabled in production" || fail "/docs exposed"
curl -sI "localhost:$PORT/healthz" | grep -qi '^x-content-type-options: nosniff' && pass "security headers present" || fail "headers"
[ "$(docker exec smoke-api id -u)" != "0" ] && pass "runs as a non-root user" || fail "running as root"

echo "workers"
run_role smoke-worker worker
run_role smoke-confirmations confirmations
run_role smoke-crm crm
sleep 12   # past the 5s idle-poll window that used to crash idle workers
for n in smoke-worker smoke-confirmations smoke-crm; do
  [ "$(docker inspect -f '{{.State.Status}}' "$n")" = "running" ] && pass "$n still running after an idle poll" || fail "$n exited"
done

echo "graceful shutdown"
for n in smoke-worker smoke-confirmations smoke-crm smoke-api; do
  docker stop -t 20 "$n" >/dev/null
  code="$(docker inspect -f '{{.State.ExitCode}}' "$n")"
  [ "$code" = "0" ] && pass "$n exited 0 on SIGTERM" || fail "$n exit code $code (137 = SIGKILLed)"
done

echo "smoke test passed"
