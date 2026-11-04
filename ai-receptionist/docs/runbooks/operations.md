# Runbook: operating the platform

What to check, in order, when something goes wrong in production, and how to
do the routine jobs safely. Setup is in `infra/railway/README.md`; connecting a
phone number is in `connecting-the-voice-system.md`; backups and restoring the
database are in `backup-and-restore.md`.

Commands below that run scripts do so from `apps/api` on a machine with the
production `DATABASE_URL` (the `receptionist_app` role) and `REDIS_URL`
exported, e.g. via `railway run` or a shell on the `api` service.

## Callers hear silence, or the agent can't do anything

1. **Is the API up?** `curl https://<api>/readyz`. It should return
   `{"status":"ready","database":"ok","redis":"ok"}`. If not, see the next
   section.
2. **Is Vapi reaching us?** In the `api` service's logs, look for
   `POST /webhooks/voice/vapi`:
   - **No requests at all:** the assistant points somewhere else.
     `PUBLIC_WEBHOOK_BASE_URL` changed (a new Railway domain?) and the
     assistant wasn't updated. Set it on `api` and `crm`, then run
     `make provision SLUG=<slug>` for each tenant.
   - **401s:** `VAPI_WEBHOOK_SECRET` differs from the one the assistant was
     provisioned with. Same fix: one value on `api` and `crm`, then
     `make provision`.
   - **500s:** read the traceback (Sentry, if `SENTRY_DSN` is set). Tool
     failures don't 500: they return a spoken fallback and log
     `tool_dispatch_failed`.
3. **Tool calls time out:** each tool is capped at `TOOL_DEADLINE_SECONDS`
   (8 s by default) and answers with a fallback instead of hanging. Repeated
   `check_availability` fallbacks mean Google Calendar (below).

## The API won't start or won't pass its healthcheck

The process logs say why. Common causes, all deliberate:
- **`UnsafeDatabaseRoleError` ("superuser=True" or "bypassrls=True"):**
  `DATABASE_URL` uses the provider's admin login. Point it at `receptionist_app` (`infra/postgres/bootstrap-managed.sql`).
  This guard is what keeps tenants' data apart; never work around it.
- **A `SECRET_KEY` validation error:** set a real key of at least 32
  characters (`openssl rand -hex 32`) on every service.
- **Migration failed in pre-deploy:** the new version never took traffic and
  the old one is still serving. Fix the migration and redeploy; don't edit
  `alembic_version` by hand.
- **`/readyz` failing:** Postgres or Redis unreachable. Check those services
  and the URLs.

## "I'm having trouble reaching the calendar"

- **One tenant:** their Google grant was revoked or expired. Settings shows
  Google Calendar as `revoked`; they reconnect it there.
- **Every tenant:** a Google outage, or `GOOGLE_OAUTH_CLIENT_*` changed.
  Bookings resume on their own when Google does; nothing is half-booked
  (the slot is only held if the calendar write succeeds).

## Background jobs aren't happening

Summaries, lead scores, confirmation texts and emails, CRM sync and
voice-agent updates all run off Redis queues:

| Queue | Consumer service | Job |
|---|---|---|
| `queue:post_call` | worker | summary, outcome, lead score (Claude) |
| `queue:confirmations` | confirmations | booking email + SMS |
| `queue:crm` | crm | HubSpot contact, activity, deal |
| `queue:voice_sync` | crm | push Settings changes to the Vapi assistant |

1. **Is the consumer running?** Each worker logs on boot:
   `post_call_worker_started`, `confirmations_worker_started` or
   `crm_worker_started`. A crashed worker restarts automatically and
   reclaims the job it was holding (`jobs_reclaimed`).
2. **How far behind is it?**
   ```bash
   redis-cli -u "$REDIS_URL" LLEN queue:confirmations           # waiting
   redis-cli -u "$REDIS_URL" LLEN queue:confirmations:processing # in hand
   redis-cli -u "$REDIS_URL" ZCARD queue:confirmations:delayed   # waiting to retry
   redis-cli -u "$REDIS_URL" LLEN queue:confirmations:dead       # given up
   ```
   A growing ready list with a healthy worker means it's slower than
   arrivals. For `worker`, that usually means Claude is slow or rate
   limiting (`LLM_TIMEOUT_SECONDS`).
3. **Retries:** transient failures retry with backoff, up to 5 attempts
   (about 20 s in total). Permanent failures (a 4xx from the provider) go
   straight to `:dead`.

### Requeuing dead jobs

Fix the cause first (reconnect HubSpot, fix the API key...), then:

```bash
python scripts/requeue_dead.py crm                        # what's dead, and why
python scripts/requeue_dead.py crm --requeue --dry-run    # what would move
python scripts/requeue_dead.py crm --requeue              # all of them
python scripts/requeue_dead.py crm --requeue --tenant <tenant-id>
```

Jobs go back with a fresh retry budget. Every handler is idempotent, so a
requeued confirmation won't text twice and a requeued CRM sync won't
duplicate a deal. Don't move dead jobs with `redis-cli`: they would keep
their spent attempt count and die again after one failure. The dead list
keeps the newest 1,000 jobs per queue. Durable failure state is in
Postgres: failed CRM syncs show in the dashboard's CRM sync page, and failed
voice updates in Settings.

## Settings says the phone agent is "not updated"

The `crm` service couldn't push the change to Vapi. The message says why:
- **"HTTP 401/403":** `VAPI_API_KEY` is missing or wrong on `crm` (it needs
  the voice variables too; see the Railway README).
- **"not an IANA timezone name":** should be impossible now that Settings
  validates it; fix the tenant's timezone.
- **Anything transient:** the owner clicks **Try again**.

The setting itself is saved either way; calls keep using the previous
version until the push lands.

## Database connections run out

Symptoms: `too many connections` from Postgres, or requests stalling then
failing with pool timeouts. Each process uses up to
`DB_POOL_SIZE + DB_MAX_OVERFLOW` (15 by default). Keep
`(api instances × WEB_CONCURRENCY + 3) × 15` under `SHOW max_connections;`
by lowering the pool settings or the process count.

## Rotating secrets

| Secret | How | Effect |
|---|---|---|
| `SECRET_KEY` | Set the new value on all services; redeploy. | Everyone is signed out of the dashboard. |
| `VAPI_WEBHOOK_SECRET` | Set it on `api` and `crm`, deploy, then `make provision` for every tenant. | Between the deploy and each tenant's provision, that tenant's webhooks are refused. Do it at a quiet time. |
| `CREDENTIALS_ENCRYPTION_KEY` | **Don't**, unless you re-encrypt `integrations` (there is no tool for that yet). | With a new key, every stored Google and HubSpot grant becomes unreadable, and every tenant must reconnect. |
| Provider keys (Anthropic, Voyage, Resend, Twilio, HubSpot, Google, Vapi) | Create the new key at the provider, set it on the services that use it (Railway README, section 3), redeploy, then revoke the old one. | None if done in that order. |
| Database password | Re-run `bootstrap-managed.sql` with a new `app_password`, update `DATABASE_URL` on all services, redeploy. | Old connections drop when the password changes, so do it with the redeploy. |

## Before a busy season: load test a staging copy

`scripts/loadtest.py` drives concurrent simulated calls through the real
webhook path and checks booking integrity afterwards. Method and results
are in `docs/load-test.md`. It needs `scripts/loadtest_server.py`: the real
API with an in-memory calendar, since its throwaway tenant has no Google
Calendar. Point that at a staging copy of the database, never production. It
creates and removes its own tenant, but it does load the database.
