# Dashboard (Next.js)

The tenant-facing web app: call history with transcripts, analytics, appointments, the
callback queue, CRM sync failures, settings (persona, timezone, Google/HubSpot connect), and
the **Test Lab** (simulated calls, a tool bench, a live per-bay day board).

## Purpose

Lets a practice's staff see what the AI receptionist did and change how it behaves, without
engineering involvement. Delivered in Phase 7 (M5).

## Stack

Next.js 15 (App Router, React 19, TypeScript strict), Tailwind CSS 4, Phosphor icons. No
client-side data fetching library and no UI kit.

## Design

Apple Human Interface style built from the ui-ux-pro-max design system. It uses the San
Francisco system font (Geist as the fallback), inset grouped lists, segmented controls,
large titles, capsule badges and a translucent sidebar. It follows the system's light or
dark appearance automatically. Every token, the contrast ratio it was measured at, and the
pre-delivery checklist are in **`design-system/MASTER.md`**. Change colours there and in
`src/app/globals.css` together.

## How it talks to the API

Every API call is made **server-side** (Server Components, Server Actions, one route
handler) with the user's access token, which lives in an `httpOnly` cookie. The browser
never sees the token and never calls the API directly — so the API needs no browser CORS
access for this app, and a script injected into a page cannot read the token.

- `src/lib/api.ts` — the one place that attaches the Bearer token, parses the API's
  `{error: {code, message}}` envelope into `ApiError`, and on a 401 sends the user through
  `/api/auth/logout` (the only place that can clear the cookie) to `/login`.
- `src/lib/types.ts` — response types, **hand-written** to mirror
  `apps/api/src/app/modules/*/schemas.py`. See Known risks.
- `src/middleware.ts` — redirects to `/login` when there is no session cookie. This is a
  convenience check only; authorization is enforced by the API on every request.
- `src/app/api/lab/[...path]/route.ts` — the Test Lab's only browser-to-API path. It is a
  same-origin proxy that adds the token server-side, because the lab is interactive (chat,
  bench, polling) and Server Actions can't poll. Path segments are allow-listed.
- `src/app/api/integrations/[provider]/connect/route.ts` — asks the API for the provider's
  authorize URL (a browser navigation cannot carry a Bearer token) and redirects there.
  The provider later redirects to the API's `/callback`, which redirects back to
  `/settings?connected=...`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `API_BASE_URL` | `http://localhost:8000` | Where the dashboard's *server* reaches the API |

The API must be configured with `DASHBOARD_BASE_URL` (this app's public URL) so the OAuth
callback can redirect back here, and `PUBLIC_WEBHOOK_BASE_URL` for the provider redirect URI.

## Running

```bash
make dashboard-install   # npm install
make dev                 # API on :8000 (separate terminal)
make dashboard-dev       # dashboard on :3000
make dashboard-build     # production build + typecheck
```

Sign in with the tenant's `dashboard:` credentials from its `config/tenants/*.yaml`
(created by `make seed`): business ID is the tenant slug, e.g. `bright-smile-dental`.

## Settings and the live phone agent

Saving in Settings stores the change at once, but callers only hear it once
the API has pushed it to the voice vendor (a background job, normally
seconds). The **Phone agent** row under Integrations shows that state: live,
updating, not updated (with **Try again**), or not connected. While an update is
in flight the page re-renders every 3 s (for up to 2 minutes, paused while the
tab is hidden), so the save notice changes from "Sending it to the phone
agent..." to "Saved and live" without a reload.

## When to take a call back

Settings → **When to take a call back** lists, one per line, the situations in
which the receptionist takes a message for the team instead of handling the
request (a vehicle that isn't safe to drive, a dispute about an invoice). They
are part of the agent's instructions, so a save goes to the phone agent like
Instructions do, and the notice follows it from "Sending..." to "Saved and live".

## Callback promise

Settings → **Callback promise** edits what callers hear after asking for a
call back: one text for opening hours and an optional one for after hours,
where `{next_open}` becomes the next opening time ("tomorrow at 7:30 am").
**Insert opening time** puts the placeholder at the cursor, so it can't be
mistyped. A preview shows both, filled in from the real business hours. A
save takes effect on the next call, with no phone-agent update to wait for.

## Roles

`viewer` can read everything. `admin` and `owner` can cancel appointments, work callbacks,
edit settings, connect integrations, and use the Test Lab. The UI hides controls a viewer cannot use; the API
is what actually refuses them (403).

## Known risks / future improvements

- **Types can drift from the API.** They are hand-written. Generate them from the FastAPI
  OpenAPI spec (`packages/shared-types`) once the surface stabilises.
- **No automated frontend tests yet.** Phase 7 and the Test Lab were verified by driving a
  production build against the API with a real browser (light, dark, 390px; no console or
  CSP errors, no horizontal overflow); that script is not committed.
- The Test Lab polls (board every 5 s, activity every 3 s, paused while the tab is hidden).
  Fine for one person testing; switch to server-sent events if many staff keep it open.
- Sessions are a single 60-minute token with no refresh; expiry means signing in again.
- Not built: knowledge-base editor, qualification-question and escalation-policy editing,
  user management, password change.
