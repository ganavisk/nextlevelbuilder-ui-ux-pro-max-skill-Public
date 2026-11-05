# Runbook: connecting a tenant to the phone network

How a tenant in our database becomes a phone number a customer can call.
Until `make provision` runs, a tenant exists in the schema but is unreachable.

## The four links

```
Twilio number ──▶ Vapi assistant ──▶ our webhook ──▶ our tools
   (you buy)      (make provision)   (PUBLIC_WEBHOOK_    (already built)
                                      BASE_URL)
```

1. **Twilio** owns the phone number.
2. **Vapi** runs the voice agent (speech in/out, turn-taking) and holds the
   number.
3. **Our webhook** receives call events and tool calls at
   `POST /webhooks/voice/vapi`.
4. **Our tools** answer FAQs, record qualification, take callbacks.

Links 3 and 4 were built in Phases 3–4. `make provision` creates link 2 and
wires it to link 1.

## One-time setup

### 1. Accounts and credentials

| What | Where it goes | Notes |
|---|---|---|
| Twilio Account SID + Auth Token | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Vapi uses these to import your number |
| A Twilio phone number | the tenant's `phone_numbers.e164` row | ~$1/month; buy in the Twilio console |
| Vapi API key | `VAPI_API_KEY` | Vapi dashboard → API keys |
| A webhook secret you invent | `VAPI_WEBHOOK_SECRET` | any random string; e.g. `openssl rand -hex 32` |
| Anthropic API key | `ANTHROPIC_API_KEY` | in-call answers + post-call summaries |
| Voyage API key | `VOYAGE_API_KEY` | knowledge-base embeddings |

### 2. Voice quality: using ElevenLabs voices

The demo tenants are configured to speak with **ElevenLabs** voices while Vapi
continues to handle telephony and turn-taking. For a receptionist this is the
caller's first impression of the business, so it's worth setting up.

One dashboard step is required: **add your ElevenLabs API key in the Vapi
dashboard → Provider Keys**. Vapi calls ElevenLabs on your behalf, so the key
lives there, not in our `.env` (our platform never calls ElevenLabs directly
in this configuration).

Then pick a voice in the ElevenLabs dashboard (Voices) and put its id in the
tenant's YAML:

```yaml
agent:
  voice_provider: 11labs               # ← selects the vendor
  voice_id: u91OxrFDaRINovYRRue2       # ← from the ElevenLabs dashboard
  voice_model: eleven_turbo_v2_5       # ← low-latency model
```

Three things worth knowing:

- **`voice_provider` is not optional.** A voice id without it is silently
  ignored and you get Vapi's default voice — the most common way this looks
  "connected but wrong".
- **Use a turbo/flash model.** The standard multilingual model adds enough
  latency to be audible on a phone call, against the sub-second budget
  (NFR-01).
- **Voice ids in the example configs are picked ElevenLabs library voices**
  ("Diane" for dental, "Mike" for legal) chosen for the persona in each
  tenant's `agent.persona` — not arbitrary stock defaults. Swap them for your
  own picks; the fit matters, since for a receptionist this voice is the
  caller's first impression of the business.

Re-run `make provision` after changing any voice setting.

### 3. A publicly reachable URL

Vapi must be able to POST to us. Set `PUBLIC_WEBHOOK_BASE_URL` to the public
origin of this API — **no trailing slash**, and it must be HTTPS.

- **Production:** your deployed host, e.g. `https://api.yourdomain.com`.
- **Local development:** an HTTPS tunnel to port 8000, e.g.
  `cloudflared tunnel --url http://localhost:8000` and use the URL it prints.

This is the single most common reason a call connects but the agent can't do
anything: if the URL is wrong or unreachable, Vapi has nowhere to send tool
calls, and the agent will improvise instead of using the knowledge base.

## Provisioning a tenant

```bash
make check-keys                  # every key works? (one cheap call each, keys never printed)
make db && make migrate          # database up to date
make seed                        # creates the demo tenant + its number row
make provision                   # ← pushes the agent to Vapi, attaches the number
make dev                         # API listening on :8000 (tunnel points here)
make worker                      # post-call summaries, in a second terminal
```

`make provision` defaults to the demo dental tenant. For any other tenant:

```bash
make provision SLUG=hartley-law
```

What it does:
1. Loads the tenant and its **active** agent config.
2. Assembles the runtime system prompt (persona + forbidden-topic guardrails +
   qualification flow + callback script) and the tool schemas.
3. Creates or **updates** the Vapi assistant, including the server URL and
   secret so webhooks reach us.
4. Imports/re-points the Twilio number at that assistant.
5. Stores `vendor_agent_id` and `vendor_number_id` on `phone_numbers`.

**It is idempotent.** Re-run it after any change to the persona, knowledge
base, qualification flow, or tool set — it updates the existing assistant
rather than creating a duplicate. If the assistant was deleted in the Vapi
dashboard, it detects the stale id and recreates it.

## Verifying the connection

Call the number. Then check, in order:

| Symptom | Likely cause |
|---|---|
| Rings, nobody answers | Number not attached — re-run `make provision`; check it appears in the Vapi dashboard |
| Agent answers but with the wrong greeting/persona | Provisioning ran before the config change — re-run `make provision` |
| Wrong voice — Vapi default instead of ElevenLabs | `voice_provider` missing from the tenant YAML, or the ElevenLabs key isn't in Vapi's Provider Keys |
| Voice sounds right but replies feel laggy | Using a non-turbo `voice_model`; switch to `eleven_turbo_v2_5` |
| Agent answers but never uses the knowledge base | Webhook unreachable. Check `PUBLIC_WEBHOOK_BASE_URL`, that the tunnel is live, and that `make dev` is running |
| Webhooks arrive but 401 | `VAPI_WEBHOOK_SECRET` differs from what was provisioned — re-run `make provision` after fixing `.env` |
| Answers are always "I'll have someone follow up" | Knowledge base not embedded — set `VOYAGE_API_KEY` and re-run `make seed` (it skips ingestion without the key) |
| Call logged but no summary/outcome | `make worker` isn't running |

Confirm the data landed:

```sql
-- transcript, cost and outcome for the most recent call
SELECT set_config('app.tenant_id', '<tenant-uuid>', false);
SELECT started_at, ended_at, outcome, sentiment, summary,
       vendor_cost_cents, llm_cost_cents
FROM calls ORDER BY started_at DESC LIMIT 1;
```

## Switching voice vendors

`VOICE_PROVIDER` selects the adapter. Only `vapi` ships today; the interface
(`modules/telephony/providers/base.py`) exists so another vendor —
Retell, ElevenLabs Agents — is one adapter file plus a line in the factory,
with no change to the conversation engine, tools, or data layer.
