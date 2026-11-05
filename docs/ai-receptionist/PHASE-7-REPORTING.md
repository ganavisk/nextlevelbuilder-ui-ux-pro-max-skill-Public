# AI Receptionist Platform — Phase 7: Reporting & Dashboard

**Status:** Delivered — awaiting product-owner approval before Phase 8 (Deployment)
**Milestone:** M5 "Reporting" — dashboard API and web app complete, integration-tested
(API) and driven end to end in a real browser (web). Live Google/HubSpot consent screens
still wait on OAuth credentials.

---

## 1. What Phase 7 delivers

A practice's staff can sign in, see what the receptionist did, work the callback queue,
cancel bookings, and change how the receptionist behaves — and connect Google Calendar and
HubSpot themselves, which until now required seeding database rows by hand.

| Component | Where | Verified |
|---|---|---|
| **Login + JWT bearer auth**, roles `viewer < admin < owner` | `core/auth.py`, `core/security.py`, `tenants/login.py` | ✅ 14 unit + 9 route |
| **Tenant + agent-config API** (PATCH deep-merges settings; agent config is versioned) | `tenants/routes.py` | ✅ route tests incl. regressions |
| **OAuth connect flow** (Google Calendar, HubSpot) with signed `state` | `tenants/oauth*.py` | ✅ 18 route/unit |
| **Calls list/detail + transcript**, appointments, callbacks, CRM failed syncs, analytics | `modules/*/routes.py` | ✅ 17 route (live Postgres) |
| Migration 0007 (`users.password_hash`) | `migrations/versions/0007_*.py` | ✅ applied |
| **Next.js dashboard** — 7 screens + login | `apps/dashboard/` | ✅ production build + real-browser run (§5) |

**Backend test count: 259 (up from 182 at Phase 6 close).** `ruff check` / `ruff format`
clean; `next build` (which type-checks, strict) clean.

## 2. API surface (all under `/api/v1`)

| Route | Min role |
|---|---|
| `POST /auth/login` | public |
| `GET /tenants/me`, `GET /tenants/me/agent-config` | viewer |
| `PATCH /tenants/me`, `PATCH /tenants/me/agent-config` | admin |
| `GET /integrations` | viewer |
| `GET /integrations/{provider}/connect` | admin |
| `GET /integrations/{provider}/callback` | none — authenticated by signed `state` |
| `GET /calls`, `GET /calls/{id}`, `GET /appointments`, `GET /callback-requests` | viewer |
| `POST /appointments/{id}/cancel`, `PATCH /callback-requests/{id}` | admin |
| `GET /crm/failed-syncs`, `GET /analytics/summary` | viewer |

List endpoints return `{items, total}` with `limit`/`offset`. Errors are always
`{error: {code, message}}`; the dashboard branches on `code`, never on message text.

## 3. Security model

- **Tokens.** HS256 JWT, 60-minute lifetime. OAuth `state` is a *different* token type
  (`typ: oauth_state`, 10 minutes) signed with the same key; `decode` checks `typ`, so a
  `state` value cannot be replayed as a login token (tested).
- **Passwords.** bcrypt. Verification fails closed on an empty or malformed stored hash.
- **No enumeration.** Wrong password, unknown email and unknown tenant slug return the
  identical 401 body (tested by comparing the bodies).
- **Tenant isolation.** Every dashboard query runs in `tenant_session`, so Postgres RLS
  applies on this path too, not just the webhook path. A live cross-tenant test confirms a
  tenant cannot see another's calls.
- **The browser never holds the API token.** The dashboard's server makes every API call; the
  token lives in an `httpOnly`, `sameSite=lax` cookie (`secure` in production).
- **Startup guard.** In staging/production the API refuses to start with the development
  default or any `SECRET_KEY` under 32 characters (see §6).

## 4. How the OAuth connect flow works

```
Browser ─▶ Dashboard GET /api/integrations/hubspot/connect     (first-party, has the cookie)
             └─▶ API GET /integrations/hubspot/connect  (Bearer, admin)  ─▶ {authorize_url}
Browser ◀─ 303 to HubSpot's consent screen (signed state = tenant + provider, 10 min)
Browser ─▶ HubSpot, user approves ─▶ API GET /integrations/hubspot/callback?code&state
             └─ verify state, exchange code, encrypt + store credentials
Browser ◀─ 303 to {DASHBOARD_BASE_URL}/settings?connected=hubspot  (or ?error=<code>)
```

`/connect` returns the URL as JSON instead of redirecting because a cross-origin browser
navigation cannot carry a Bearer token. Every exit from `/callback`, including failures, is a
redirect to the dashboard — the user arrived by full-page navigation, so a JSON error body
would just render as raw text.

## 5. Verification performed (this session)

**Backend:** 259 tests pass against live Postgres + Redis.

**Frontend** was verified by building for production, starting it against the live API with a
seeded demo tenant (46 calls, 4 appointments, 3 callbacks, 1 failed CRM sync), and driving it
with Chromium. Confirmed:

- An unauthenticated visit to `/calls` redirects to `/login`; a wrong password shows an
  inline error and keeps the business ID and email; the right one lands on the overview.
- Every screen renders real API data; the call detail shows the transcript turns.
- **Mutations persist** — resolving a callback and saving a new greeting were checked in the
  database / by the agent-config version number increasing, not just by the UI updating.
- Connect with a test client id redirected to `https://app.hubspot.com/oauth/authorize?...`.
- Sign-out clears the session and `/calls` redirects to `/login` again.
- No browser console errors; no page-level horizontal overflow at 390px wide on any screen.

Screens were also reviewed visually (login, overview, calls, call detail with and without a
transcript, appointments, settings, loading skeleton).

## 6. Defects found and fixed this phase

**`PATCH /tenants/me` silently deleted nested settings.** A shallow merge replaced a whole
nested object, so changing `settings.crm.enabled` erased `settings.crm.deal_pipeline`. Found
with a live Postgres smoke test, fixed with a recursive `deep_merge`, covered by unit and
route-level regression tests.

**Viewers could cancel appointments and change callbacks through the API.** Both routes
required only a valid login; the dashboard hid the buttons, which is not enforcement. Both
now require `admin`. The two regression tests fail against the old code (verified).

**A deployed API could sign login tokens with a public key.** `SECRET_KEY` defaulted to
`dev-only-secret`, which is in this repository; a production process started without the
variable would have accepted forged tokens for any tenant. Staging/production now refuse to
start unless the key is non-placeholder and at least 32 characters (tested).

**Migrations broke on a fresh install, and would have in production.** SQLAlchemy 2.1 changed
the default driver for a bare `postgresql://` URL from `psycopg2` to `psycopg` v3. Fifteen
places derived a sync URL that way (Alembic plus 14 test files); all now name
`postgresql+psycopg2://`. Separately, `psycopg2-binary` was a dev-only dependency while the
production image installs without dev extras, so `make migrate` would have failed there
regardless — it is now a base dependency.

**Failed login wiped the form.** React 19 resets uncontrolled inputs after a form action
completes, so a mistyped password also cleared the business ID and email. The action now
echoes the non-secret values back.

**Tables overflowed the page on phones.** An `sr-only` "Actions" header is absolutely
positioned and escaped the unpositioned scroll wrapper, widening the whole page. The wrappers
are now `relative`.

Also: FastAPI's `Depends()` in argument defaults trips ruff B008; scoped to the route and auth
files via `per-file-ignores` rather than disabling the rule project-wide.

## 7. Design notes for the reviewer

- **Server-side API calls only.** Costs one extra hop and means no CORS surface for the
  dashboard; in exchange a cross-site script cannot read the token.
- **`middleware.ts` is a convenience, not a control.** It only checks that a cookie exists.
  Authorization is the API's job on every request; an expired token produces a 401 that
  routes through `/api/auth/logout` to clear the cookie (Server Components cannot).
- **Viewer UI hides controls; the API enforces.** Both layers exist on purpose.
- **Agent config is versioned, never edited in place.** Saving flips `is_active` and bumps the
  version, so a bad prompt edit is recoverable from the previous row.
- **Design is calibrated for working software, not a marketing page:** left-aligned, dense,
  tables over cards, one desaturated teal accent on zinc, monospace for numbers, skeleton
  loading, explicit empty and error states, CSS-only motion.

## 8. What the live demo still needs from you

| Need | Why | Where it goes |
|---|---|---|
| Google OAuth client id + secret | Calendar connect screen | `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` |
| HubSpot OAuth client id + secret | HubSpot connect screen | `HUBSPOT_CLIENT_ID` / `_SECRET` |
| Public API URL | provider redirect URI | `PUBLIC_WEBHOOK_BASE_URL`; register `{it}/api/v1/integrations/{provider}/callback` with each provider |
| Public dashboard URL | post-connect redirect | `DASHBOARD_BASE_URL` |
| A real `SECRET_KEY` | login tokens | `openssl rand -hex 32` |

## 9. Known gaps carried into Phase 8

- **No rate limiting or lockout on `/auth/login`.** bcrypt slows guessing but does not stop
  it. This should be closed before real tenants use it.
- **No automated frontend tests.** The browser verification in §5 was a manual run; the script
  is not committed.
- **Frontend types are hand-written** and can drift from the API. Generate from OpenAPI.
- **Sign-out only clears the cookie.** The token itself stays valid until it expires (60
  minutes). There is no revocation list and no refresh.
- **OAuth is verified up to the provider's consent screen**, not through a live token exchange.
- Not built: knowledge-base editor, editing qualification questions / escalation policy, user
  management, password change.
- The demo owner password in the tenant YAML is for local seeding only.

## 10. Next: Phase 8 — Deployment (on your approval)

Railway/Vercel deploy blueprints, production configuration and secrets, CI, and the hardening
items above (login rate limiting first).
