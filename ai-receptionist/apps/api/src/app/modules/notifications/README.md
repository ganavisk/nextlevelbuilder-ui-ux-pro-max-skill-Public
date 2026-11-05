# notifications

**Purpose:** Outbound email and SMS — appointment confirmations today,
reminders next. Every message is sent at most once, no matter how often its
job is redelivered. Vendors sit behind `EmailProvider` / `SMSProvider` and
are replaceable.

**Dependencies:** `core` (db, config, logging, queue),
`tenants` (the `Tenant` row supplies the business name and the
`settings.notifications` policy block), `scheduling` (reads the
`appointments` row a confirmation refers to). Driven by
`app.workers.confirmations`, never from the call path.

**Inputs:** a job `{type, tenant_id, appointment_id}` from
`queue:confirmations`.
**Outputs:** a `notifications` row per channel (`pending` → `sent`/`failed`),
and the message itself at the vendor.

## Send-at-most-once

The ordering is the whole design. Each notification row is inserted `pending`
with a unique idempotency key **before** the provider is called, via
`INSERT ... ON CONFLICT DO NOTHING ... RETURNING id`. The send proceeds only
if this worker won that insert:

```
claim (insert, unique key)  ──► lost?  stop, someone else is sending it
        │ won
        ▼
    call the vendor
        │
        ▼
 finalize (sent / failed)
```

A retried job therefore finds the row already claimed and returns without
sending. Doing it the other way round — send, then record — means any crash
between the two sends the customer a duplicate on the next redelivery, which
is the failure mode customers actually notice.

Keys are scoped to the appointment and channel (`appt:<id>:email`,
`appt:<id>:sms`), so the two channels are independent claims.

## Channels fail independently

Email and SMS are dispatched separately. A Twilio outage must not cost the
customer their email confirmation, so one channel's failure marks only its
own row `failed` and does not suppress the other.

## Running it

Its own process, separate from the post-call worker:

```bash
make confirmations      # python -m app.workers.confirmations
```

Kept separate on purpose: confirmations must keep draining even when
post-call summarization is backed up behind a slow LLM.

**Configuration:**

| Setting | Where | Notes |
| --- | --- | --- |
| `RESEND_API_KEY` | env | email vendor |
| `EMAIL_FROM` | env | must be a verified sender domain |
| `TWILIO_ACCOUNT_SID` / `_AUTH_TOKEN` | env | SMS vendor |
| `TWILIO_MESSAGING_SERVICE_SID` | env | preferred over a bare from-number |
| `notifications.email_confirmation` | tenant settings | default true |
| `notifications.sms_confirmation` | tenant settings | default true |
| `notifications.sms_reminder_hours_before` | tenant settings | not yet consumed — reminders are future work |

**Future improvements:** appointment reminders on a scheduled sweep (the
`sms_reminder_hours_before` setting exists for this); retry with backoff on
transient vendor failures, which currently just mark `failed`; delivery-status
webhooks from Resend/Twilio to close the loop on bounces; per-tenant sender
identities; templates in the tenant's own wording.

**Known risks:**
- SMS is transactional-only and carries "Reply STOP to opt out" — a TCPA
  constraint, not a style choice. Any future marketing use needs a separate
  consent path and must not reuse this module's providers.
- A `failed` row is currently terminal: nothing retries it and nothing alerts
  on it. That is acceptable only while volume is low, and is the first thing
  to fix before onboarding real practices.
- Both adapters are tested against fakes, not live vendors. Their wire format
  is unverified until the M3 live test.
