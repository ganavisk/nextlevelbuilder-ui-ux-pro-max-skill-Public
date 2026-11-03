# AI Receptionist Platform

Multi-tenant, voice-first AI receptionist for legal and dental practices.
Answers calls, answers FAQs from a per-tenant knowledge base, qualifies leads,
books appointments, syncs CRM, and sends confirmations — configured per
business, never custom-coded per business.

> **Note:** This directory is designed to become the root of the dedicated
> `ai-receptionist` repository. Until that repo exists it lives inside the
> planning repo; everything under this folder is self-contained.

## Architecture (summary)

- **Voice layer (vendor):** Vapi on Twilio numbers — ASR, TTS, turn-taking.
  Isolated behind a `VoiceProvider` interface; Retell AI is the fallback adapter.
- **Platform (owned):** FastAPI modular monolith — 8 bounded modules
  (`telephony`, `conversation`, `scheduling`, `crm`, `notifications`,
  `tenants`, `analytics`, `escalation`), each replaceable behind a provider
  interface.
- **AI:** Claude API — Sonnet for in-call reasoning, Haiku for classification
  and summaries. Voyage AI for knowledge-base embeddings.
- **Data:** PostgreSQL 16 + pgvector (RLS-enforced multi-tenancy), Redis for
  call state and the post-call job queue.
- **v1 escalation policy:** voicemail-plus-callback (no live transfer).
- **Language:** English only at launch.

Full design documents: `../docs/ai-receptionist/` (one per phase; Phase 7 covers
the dashboard API and web app).

## Prerequisites

- Docker + Docker Compose
- Python 3.12+ (for running the API outside Docker)
- Make

## Quick start

```bash
cp .env.example .env          # then fill in secrets (see Phase 2 doc §4)
make up                       # postgres + redis + api via docker compose
make migrate                  # apply database migrations
curl localhost:8000/healthz   # -> {"status":"ok"}
curl localhost:8000/readyz    # -> {"status":"ready", ...} (checks DB + Redis)
```

Local development without Docker for the API:

```bash
make install                  # creates .venv and installs apps/api
make check-keys               # confirms each key in .env with its provider
make db                       # starts only postgres + redis in Docker
make migrate
make dev                      # uvicorn with reload on :8000
make test
make lint
```

The background workers are separate processes — run each in its own terminal:

```bash
make worker                   # post-call: transcripts, summaries, lead scoring
make confirmations            # appointment confirmation email + SMS
make crm                      # CRM sync (HubSpot): contact, activity, deal
```

The dashboard (Phase 7) is a separate Next.js app:

```bash
make dashboard-install        # npm install
make dashboard-dev            # http://localhost:3000, talks to the API on :8000
```

In staging/production the API refuses to start unless `SECRET_KEY` is a random
value of at least 32 characters (`openssl rand -hex 32`) — it signs dashboard
login tokens.

They are deliberately not one process: confirmations must keep draining even
when post-call summarization is backed up behind a slow LLM, and a tenant's
CRM being down must never delay a confirmation a customer is waiting on. All
three share the retry/backoff/dead-letter queue in `core/queue.py` — a job a
handler fails to process is retried with jitter, not lost.

> **Database roles.** The application connects as `receptionist`, which is
> **not** a superuser. This is load-bearing, not incidental: Postgres
> superusers bypass row-level security unconditionally, so running the app as
> one silently voids every tenant-isolation policy while leaving them looking
> correct in `\d`. Compose bootstraps as `postgres` and creates the app role
> via `infra/postgres/init/`. `tests/test_tenant_isolation.py` fails loudly if
> this is ever undone.

## Repository layout

```
apps/api/          FastAPI modular monolith (source of truth for the backend)
apps/dashboard/    Next.js dashboard (call history, analytics, settings) — see its README
config/tenants/    Example tenant configurations (dental, legal)
infra/postgres/    Container init: extensions + the non-superuser app role
infra/             Deploy blueprints (Railway/Vercel) — populated in Phase 8
.github/workflows/ CI (activates when this folder becomes the repo root)
```

## Documentation standard

Every module ships a `README.md` covering: purpose, dependencies, inputs,
outputs, configuration, future improvements, and known risks.
