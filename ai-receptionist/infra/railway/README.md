# Deploying: Railway (API + workers) and Vercel (dashboard)

```
            Vapi ──webhooks──▶ ┌──────────── Railway project ─────────────┐
                               │  api ──┐                                 │
  Vercel: dashboard ──HTTPS──▶ │        ├──▶ Postgres (pgvector)          │
   (apps/dashboard)            │  worker ┤                                │
                               │  confirmations ├──▶ Redis (queues, call │
                               │  crm ───┘          state)               │
                               └──────────────────────────────────────────┘
```

One Docker image (`apps/api/Dockerfile`) runs as four Railway services; the
role is the start command's argument (`docker-entrypoint.sh <role>`). Only
`api` serves HTTP and only `api` runs migrations.

> **Paths.** The platform lives in the `ai-receptionist/` folder of this
> repository, so every Root Directory below starts with `ai-receptionist/`.
> If that folder ever becomes a repository of its own, drop the prefix.
> Railway's Root Directory is relative to the repository root, not to this
> folder: with a missing prefix, Railway finds no Dockerfile and the build fails.

> **Railway Config as Code is going away.** Railway has deprecated
> `railway.toml`/`railway.json`: new services can't opt in, and existing files
> stop being read on **2026-12-01**. Its replacement is Infrastructure as Code
> (`.railway/railway.ts`). The `*.toml` files in this folder are therefore a
> **reference** for each service's settings, not something Railway reads.
> Enter the settings below in each service's dashboard, or port them to
> `.railway/railway.ts` after checking Railway's current docs for its syntax.

## 1. Data stores

**Postgres with pgvector.** The knowledge base needs the `vector` extension
and the per-bay booking constraint needs `btree_gist`. Some providers' plain
Postgres doesn't ship pgvector; use a pgvector image or template, and confirm
with `CREATE EXTENSION vector;` before going further.

Then create the application role, as the provider's admin login, once:

```bash
psql "$ADMIN_DATABASE_URL" -v app_password="$(openssl rand -hex 24)" \
     -f infra/postgres/bootstrap-managed.sql
```

The app connects as `receptionist_app`, **never** as the admin login.
Managed providers hand out superusers, and superusers bypass row-level
security, so tenants could read each other's data. In staging/production the
API and workers check this at startup and refuse to run as a superuser or
BYPASSRLS role. Note this means `DATABASE_URL` must not reference the
Postgres service's own URL variable; build it for `receptionist_app`. Any of
`postgres://`, `postgresql://` or `postgresql+asyncpg://` is accepted.

**Or run Postgres yourself, with backups to a bucket.** If the managed
Postgres has no point-in-time recovery, deploy `infra/postgres/image` as its
own service. That's Postgres 16, pgvector and pgBackRest, with the backup
scheduler built in. Set it up like this (not yet tried on Railway itself):
- **Root Directory** `ai-receptionist/infra`; Dockerfile
  `postgres/image/Dockerfile` (the `RAILWAY_DOCKERFILE_PATH` variable). If
  the build says it can't find the Dockerfile, use the full path
  `ai-receptionist/infra/postgres/image/Dockerfile`. Keep the Root Directory
  as it is either way: the Dockerfile copies files from `pgbackrest/` and
  `postgres/`, which must be inside the build context.
- **A volume** mounted at `/var/lib/postgresql/data`.
- **No public domain or TCP proxy.** The API reaches it over the private
  network.
- **Variables:** the `PGBACKREST_REPO1_*` bucket settings,
  `POSTGRES_PASSWORD`, and `APP_DB_PASSWORD`, which creates
  `receptionist_app` on first boot.

Restores, monitoring and every variable are in
`docs/runbooks/postgres-image.md`.

**Redis.** Queues, call state and the login throttle. A managed Redis is
fine; set `REDIS_URL`. The workers' consumer connections are built to
survive idle-connection reaping (`core/queue.py: worker_redis`).

## 2. The services

Create one service per row. All four build from this repo with **Root
Directory `ai-receptionist/apps/api`** and the **Dockerfile** builder.

| Service | Start command | Pre-deploy | Healthcheck | Restart | Public domain |
|---|---|---|---|---|---|
| `api` | `docker-entrypoint.sh api` | `docker-entrypoint.sh migrate` | `/readyz`, 60 s | on failure, 10 retries | **yes** (Vapi and the dashboard call it) |
| `worker` | `docker-entrypoint.sh worker` | none | none | always | no |
| `confirmations` | `docker-entrypoint.sh confirmations` | none | none | always | no |
| `crm` | `docker-entrypoint.sh crm` | none | none | always | no |

- `/readyz` checks Postgres and Redis, so a deploy with broken dependencies
  never takes traffic. `/healthz` is plain liveness.
- `worker` runs post-call summaries and lead scoring (Claude).
  `confirmations` sends booking emails and texts. `crm` syncs HubSpot **and**
  pushes Settings changes to the Vapi assistant (`queue:voice_sync`).
- Turn on **Wait for CI** so a deploy waits for `.github/workflows/ci.yml`
  (lint, types, tests against Postgres and Redis, a production image smoke
  test).

## 3. Environment variables

Set shared values once as Railway shared variables and reference them from
each service. "All" means every one of the four services: each process
validates its configuration at startup.

| Variable | Needed by | Notes |
|---|---|---|
| `APP_ENV` | all | `production` (or `staging`). Turns on the startup guards. |
| `DATABASE_URL` | all | The **`receptionist_app`** role (section 1). |
| `REDIS_URL` | all | |
| `SECRET_KEY` | all | ≥ 32 random characters (`openssl rand -hex 32`); every process refuses the dev default. Signs dashboard logins: rotating it signs everyone out. |
| `CREDENTIALS_ENCRYPTION_KEY` | api, crm | Fernet key for stored OAuth tokens (Google, HubSpot). **Never rotate it without re-encrypting** `integrations`: the old tokens become unreadable. |
| `LOG_LEVEL`, `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE`, `APP_RELEASE` | all | Sentry is optional and scrubs call content. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | all | Connections per process; see section 5. Defaults 5 + 10. |
| `VOICE_PROVIDER`, `VAPI_API_KEY`, `VAPI_WEBHOOK_SECRET` | api, crm | The secret is any random string; Vapi echoes it on every webhook. |
| `PUBLIC_WEBHOOK_BASE_URL` | api, crm | The `api` service's public URL, e.g. `https://api-production-xxxx.up.railway.app`. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | api, crm, confirmations | Vapi imports numbers with these; confirmations sends texts. |
| `TWILIO_MESSAGING_SERVICE_SID` | confirmations | |
| `RESEND_API_KEY`, `EMAIL_FROM` | confirmations | |
| `ANTHROPIC_API_KEY`, `LLM_MODEL_PRIMARY`, `LLM_MODEL_FAST`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | api, worker | api: in-call FAQ answers and the Test Lab; worker: post-call summaries. |
| `VOYAGE_API_KEY`, `EMBEDDING_*` | api | Knowledge-base retrieval. |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | api | Calendar connect and booking. |
| `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET` | api, crm | api: the connect flow; crm: token refresh. |
| `DASHBOARD_BASE_URL`, `CORS_ORIGINS` | api | The Vercel URL; OAuth callbacks redirect back to it. |
| `TOOL_DEADLINE_SECONDS`, `MAX_CALL_DURATION_SECONDS`, `TENANT_DAILY_SPEND_CAP_USD` | api | Defaults are sensible (8 s, 900 s, $50). |
| `WEB_CONCURRENCY` | api | uvicorn processes per instance; see section 5. |
| `FORWARDED_ALLOW_IPS` | api | Defaults to `*` (Railway's edge address isn't stable). |

`.env.example` documents every variable. To check keys against their
providers before deploying, run `make check-keys` with the same values
exported (it never prints more than the last 4 characters).

## 4. First deploy, in order

1. Create Postgres (with pgvector) and Redis; run the bootstrap SQL.
2. Create `api` with the settings from section 2 and its variables. Deploy:
   the pre-deploy step runs `alembic upgrade head`, then `/readyz` must pass.
   Generate its public domain and set `PUBLIC_WEBHOOK_BASE_URL` to it.
3. Create `worker`, `confirmations` and `crm`.
4. **Vercel:** import the repo with Root Directory `ai-receptionist/apps/dashboard` (Next.js;
   `vercel.json` pins `npm ci` and `npm run build`). Set `API_BASE_URL` to the
   `api` URL. Back on Railway, set `DASHBOARD_BASE_URL` and `CORS_ORIGINS` on
   `api` to the Vercel URL.
5. Check: `curl https://<api>/readyz` returns `{"status":"ready",...}`.
6. Onboard a tenant: seed it (`scripts/seed_demo.py` with `DATABASE_URL` for
   the app role), then connect its number with
   `make provision SLUG=<slug>`. After that, Settings changes reach the phone
   agent by themselves (`docs/runbooks/connecting-the-voice-system.md`).

## 5. Sizing

From the load test (`docs/load-test.md`), on one 4-core machine that was also
running Postgres and the load generator:
- one API process handled ~60–75 webhook requests/s;
- four processes handled ~100–180/s.

A real call sends 5–8 webhooks over a few minutes, so request rate is not the
limit at any plausible number of shops. What matters on a phone line is
latency under a burst. With 4 processes, 25 callers firing back to back got
check-availability at p95 0.4 s and bookings at p95 0.3 s.

Start with **one `api` instance, `WEB_CONCURRENCY=2`**, and one of each
worker. Raise `WEB_CONCURRENCY` before adding instances.

**Database connection budget.** Each process can open up to
`DB_POOL_SIZE + DB_MAX_OVERFLOW` connections (15 by default):

```
(api instances × WEB_CONCURRENCY + 3 worker services) × 15  <  Postgres max_connections
```

The starting setup above needs up to (2 + 3) × 15 = 75. Check the limit with
`SHOW max_connections;`, leave room for migrations and your own `psql`
sessions, and lower `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` if you scale out.

## 6. Updates and rollback

- **Deploy:** merge to the connected branch; Railway builds after CI passes.
  `api` migrates in its pre-deploy step before the new version takes traffic,
  so migrations must be backward compatible with the version still running
  (add columns with defaults; drop columns a release later). No migration so
  far drops a column.
- **Roll back code:** redeploy the previous deployment from the service's
  Deployments tab. Do this for `api` and the workers together if a queue
  job's shape changed.
- **Roll back a migration:** only on purpose, from a machine with the app
  role's `DATABASE_URL`: `cd apps/api && alembic downgrade <revision>`.
  Migrations 0008–0012 were checked by upgrading, downgrading and upgrading
  again in development. A downgrade can drop data added since (e.g. `0011`
  drops callback names), so take a backup first.
- **Migration 0012 builds indexes `CONCURRENTLY`.** Writes aren't blocked,
  but each build waits for transactions already open, so run it at a quiet
  time. A failed build leaves an `INVALID` index that a re-run would skip:
  find it with `SELECT indexrelid::regclass FROM pg_index WHERE NOT
  indisvalid;`, drop it, and redeploy. Background and measurements are in
  `docs/ai-receptionist/PHASE-8-PRODUCTION.md`, section 3.
- **Backups:** Postgres needs **point-in-time recovery**, not just daily
  snapshots, which could lose a day of appointments. Use the provider's PITR
  if it has one (check Railway's current docs; this is unverified). Otherwise
  run Postgres yourself with pgBackRest to an offsite bucket:
  `infra/pgbackrest/` has the setup, scheduled backup, monitoring and restore
  scripts. A managed Postgres won't accept its `archive_command`, so on
  Railway that means a Postgres service from `infra/postgres/image` (see
  section 1). Procedure, the verification tool and the drill log are in
  `docs/runbooks/backup-and-restore.md`. Redis holds only queues and call
  state; losing it loses queued jobs, not records.

Operating the running system (incidents, queues, rotating secrets) is in
`docs/runbooks/operations.md`.
