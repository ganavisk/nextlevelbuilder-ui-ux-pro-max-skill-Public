#!/bin/sh
# Role selector for the single production image.
#   api            uvicorn on $PORT (Railway injects it), default 8000
#   worker         post-call classification worker
#   confirmations  appointment confirmation email/SMS worker
#   crm            CRM sync + voice-agent sync worker
#   migrate        alembic upgrade head (run as the pre-deploy command)
#   healthcheck    used by the image HEALTHCHECK; passes for non-API roles
#
# `exec` so the process is PID 1 and receives SIGTERM directly.
set -eu

role="${1:-api}"
# Recorded so the image HEALTHCHECK (a separate process in the container) knows
# whether this container serves HTTP.
printf '%s' "$role" > /tmp/role 2>/dev/null || true

case "$role" in
  api)
    # WEB_CONCURRENCY (read by uvicorn) sets the worker-process count.
    # --proxy-headers makes request.client reflect X-Forwarded-For from the
    # platform's edge; FORWARDED_ALLOW_IPS defaults to '*' because the edge's
    # address is not stable on Railway. Treat per-IP limits as best-effort.
    exec uvicorn app.main:app \
      --host 0.0.0.0 --port "${PORT:-8000}" \
      --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
    ;;
  worker)        exec python -m app.workers.post_call ;;
  confirmations) exec python -m app.workers.confirmations ;;
  crm)           exec python -m app.workers.crm_sync ;;
  migrate)       exec alembic upgrade head ;;
  healthcheck)
    # Only the API serves HTTP; for the other roles this image has no
    # liveness probe of its own.
    if [ "$(cat /tmp/role 2>/dev/null || echo api)" != "api" ]; then exit 0; fi
    exec python -c "import os,sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=3).status == 200 else 1)"
    ;;
  *) exec "$@" ;;
esac
