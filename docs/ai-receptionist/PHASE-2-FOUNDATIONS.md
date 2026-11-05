# AI Receptionist Platform — Phase 2: Repository Foundations

**Status:** Delivered — awaiting product-owner approval before Phase 3 (Core Engine)
**Depends on:** Phase 1 (approved 2026-07-13) with decisions: verticals = **legal + dental**;
escalation = **voicemail-plus-callback** for v1; **English-only** at launch; **dedicated
repository** planned.

---

## 1. What Phase 2 delivers

A working scaffold under `ai-receptionist/` — self-contained and designed to be lifted
verbatim into the dedicated repository:

| Deliverable | Where | Verified |
|---|---|---|
| Monorepo structure (api, dashboard placeholder, tenant configs, CI) | `ai-receptionist/` | — |
| Configuration system (env-driven, validated at startup) | `apps/api/src/app/core/config.py` | ✅ unit tests |
| Environment variable contract | `.env.example` (§4 below) | — |
| Structured JSON logging with call/tenant correlation | `core/logging.py` | ✅ |
| Error hierarchy with machine-readable codes | `core/errors.py` | ✅ |
| Async DB layer with **RLS tenant-scoped sessions** | `core/db.py` | ✅ live test (§5) |
| Webhook HMAC verification + credential encryption (Fernet) | `core/security.py` | ✅ unit tests |
| App factory with `/healthz` (liveness) + `/readyz` (readiness) | `main.py` | ✅ against live PG+Redis |
| **Full database schema** (16 tables, pgvector, RLS on all tenant tables) | `migrations/versions/0001_initial_schema.py` | ✅ applied to Postgres 16 |
| Provider interfaces (the replaceability contracts) | `modules/*/providers/base.py` | — |
| Example tenant configs for both verticals | `config/tenants/example-{dental,legal}.yaml` | — |
| docker-compose dev environment + Makefile | root | — |
| CI pipeline (lint → migrate → test with real PG/Redis) | `.github/workflows/ci.yml` | activates in dedicated repo |

**No business logic yet** — module directories contain only their provider interfaces.
The conversation engine, tool executor, and webhook handlers are Phase 3/4 work; building
them now would front-run your approval of these foundations.

## 2. Repository structure (as built)

```
ai-receptionist/
├── README.md                     # quick start + layout
├── Makefile                      # install / up / db / dev / migrate / test / lint
├── docker-compose.yml            # pgvector:pg16 + redis:7 + api
├── .env.example                  # the environment contract (all variables)
├── .github/workflows/ci.yml     # lint → migrate → pytest against service containers
├── apps/
│   ├── api/
│   │   ├── pyproject.toml        # deps, ruff, mypy strict, pytest config
│   │   ├── Dockerfile
│   │   ├── alembic.ini
│   │   ├── migrations/versions/0001_initial_schema.py
│   │   ├── src/app/
│   │   │   ├── main.py           # app factory, health endpoints, router mounting point
│   │   │   ├── core/             # config · logging · errors · db (RLS) · security
│   │   │   ├── modules/          # 8 bounded modules; providers/base.py = swap contracts
│   │   │   └── workers/          # post-call queue consumers (Phase 4+)
│   │   └── tests/                # 9 passing unit tests
│   └── dashboard/                # placeholder — scaffolded in Phase 7
└── config/tenants/               # example dental + legal configurations
```

## 3. Database schema

Sixteen tables; `tenants` is the root, everything else carries `tenant_id` and is
RLS-protected. Highlights:

- **`tool_invocations`** — the audit spine: every side effect stores request, response,
  status, and a globally unique `idempotency_key` (satisfies NFR-06 auditability and
  NFR-08 no-double-booking).
- **`callback_requests`** — first-class table for the v1 escalation policy
  (voicemail-plus-callback): caller, reason, voicemail transcript, preferred window,
  open/contacted/resolved workflow. This becomes the dashboard's callback queue.
- **`knowledge_chunks`** — pgvector `vector(1024)` with an HNSW cosine index
  (Voyage AI `voyage-3.5` embeddings; Claude doesn't provide embeddings, Voyage is
  Anthropic's recommended pairing).
- **`agent_configs`** — versioned per tenant with a partial unique index enforcing
  exactly one active config; prompt changes are rollbackable.
- **`calls`** — carries `vendor_cost_cents` + `llm_cost_cents` so per-tenant cost
  metering (NFR-09) is a column sum, not a log-scraping project.

### Row-level security model

- Policy on all 14 tenant-scoped tables: rows visible/writable only when
  `tenant_id = current_setting('app.tenant_id')::uuid`.
- `FORCE ROW LEVEL SECURITY` — policies apply even to the table owner.
- The application sets the variable per-transaction via
  `tenant_session(tenant_id)` (`SET LOCAL`, so pooled connections never leak context).
- **One-time bootstrap (documented, deliberate):** `CREATE EXTENSION vector/pgcrypto`
  requires superuser on stock Postgres. Managed platforms (Railway/Neon) either
  pre-install or allow it; for self-managed instances run the two `CREATE EXTENSION`
  statements as superuser once before `alembic upgrade head`.

## 4. Environment variable contract

Grouped as in `.env.example` — this is the complete runtime surface:

| Group | Variables | Notes |
|---|---|---|
| App | `APP_ENV`, `LOG_LEVEL`, `API_HOST`, `API_PORT`, `SECRET_KEY`, `CORS_ORIGINS` | validated at startup |
| Data | `DATABASE_URL`, `REDIS_URL`, `CREDENTIALS_ENCRYPTION_KEY` | Fernet key encrypts tenant OAuth tokens at rest |
| Voice | `VOICE_PROVIDER`, `VAPI_API_KEY`, `VAPI_WEBHOOK_SECRET` | `VOICE_PROVIDER` selects the adapter (vapi/retell) |
| Telephony | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_MESSAGING_SERVICE_SID` | SMS + number management |
| LLM | `ANTHROPIC_API_KEY`, `LLM_MODEL_PRIMARY=claude-sonnet-5`, `LLM_MODEL_FAST=claude-haiku-4-5` | Sonnet 5: $3/$15 per MTok (intro $2/$10 through 2026-08-31); Haiku 4.5: $1/$5 |
| Embeddings | `VOYAGE_API_KEY`, `EMBEDDING_MODEL=voyage-3.5`, `EMBEDDING_DIMENSIONS=1024` | knowledge-base RAG |
| Integrations | `GOOGLE_OAUTH_CLIENT_*`, `HUBSPOT_CLIENT_*`, `RESEND_API_KEY`, `EMAIL_FROM` | per-tenant tokens live encrypted in `integrations` table |
| Observability | `SENTRY_DSN` | |
| Guardrails | `MAX_CALL_DURATION_SECONDS=900`, `TENANT_DAILY_SPEND_CAP_USD=50` | cost blowout mitigation (Risk R6) |

Secrets never live in code or the database (except tenant OAuth tokens, which are
Fernet-encrypted with a key held only in the environment).

## 5. Verification performed (this session)

All run against the actual scaffold, not claimed from inspection:

1. `pip install -e "apps/api[dev]"` — clean install.
2. `pytest` — **9/9 tests pass** (config parsing, env overrides, health endpoint,
   docs-disabled-in-production, HMAC verification incl. tamper rejection, Fernet
   round-trip).
3. `ruff check` + `ruff format` — clean.
4. `alembic upgrade head` against a real Postgres 16 with pgvector — all 16 tables,
   indexes, and policies created.
5. **RLS isolation test:** seeded two tenants; tenant B saw **0** of tenant A's calls,
   and an attempt by tenant B to insert a row for tenant A was **rejected by policy**
   (`new row violates row-level security policy`). Tenant A saw exactly its own row.
6. `/healthz` → `{"status":"ok"}`; `/readyz` → `{"database":"ok","redis":"ok"}` against
   live Postgres + Redis.

Note: local verification ran on Python 3.11 (this environment's interpreter);
Docker image and CI pin 3.12. `requires-python` is set to `>=3.11` to keep both valid.

## 6. Tenant configuration surface (verticals: dental + legal)

`config/tenants/example-dental.yaml` and `example-legal.yaml` document the full
per-tenant configuration: persona and first message (with recording-consent line),
forbidden topics (no clinical advice / no legal advice — the hallucination guardrail
from Risk R3), qualification question flows with scoring (pain → urgency for dental,
deadline → urgency for legal), services and durations, and the voicemail-plus-callback
escalation policy with per-vertical triggers (including the legal-specific
"opposing party call" conflict trigger). Onboarding tenant #2 in either vertical is
editing one of these — zero code.

## 7. Dedicated repository migration plan

When you create the `ai-receptionist` repo (GitHub → New repository, empty):

```bash
# from a fresh clone of this planning repo
git subtree split --prefix=ai-receptionist -b receptionist-export
git clone <new-repo-url> ai-receptionist-repo
cd ai-receptionist-repo
git pull ../nextlevelbuilder-ui-ux-pro-max-skill-Public receptionist-export
git push origin main
```

This preserves the commit history of the scaffold. The `.github/workflows/ci.yml`
activates automatically once the folder is the repo root. The two Phase design docs
should move to `docs/architecture/` in the new repo. I can execute this migration
when you've created the empty repository and granted this session access to it.

## 8. Testing & security posture at this phase

- **Tests now:** unit (config, security, health). **Phase 3 adds:** webhook contract
  tests with recorded Vapi payloads, tenant-resolution tests, RLS isolation as an
  automated CI test (currently verified manually, §5.5).
- **Security now:** HMAC-verified webhook helper, Fernet credential encryption,
  DB-enforced tenant isolation, docs disabled in production, non-superuser DB role.
- **Deferred intentionally:** dashboard JWT auth (Phase 7), rate limiting (Phase 3,
  with the webhook surface), Sentry wiring (Phase 8).

## 9. Next: Phase 3 — Core Engine (on your approval)

Milestone M1 "First Call": Vapi adapter (webhook verification, event normalization,
agent sync), tenant resolution by inbound number, call + transcript persistence,
Redis call-session state, and the post-call worker skeleton. Demo: **call a real
phone number, the AI answers with the tenant's persona, and the call appears in the
database with its transcript.** Requires: Vapi + Twilio accounts (sandbox keys) —
have those ready if you want the live demo at the end of Phase 3.
