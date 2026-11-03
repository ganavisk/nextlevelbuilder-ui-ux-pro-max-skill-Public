# testlab

Rehearse calls without a phone. Backs the dashboard's **Test Lab** page.

## Purpose

Lets a business owner (or us, during onboarding) check that the receptionist
answers, qualifies and books correctly *before* a real number is pointed at
it — and lets anyone test scheduling capacity deterministically.

Three tools, one page:

1. **Simulated call.** You type as the caller; the tenant's **real** agent
   answers: the same assembled system prompt (`tenants.service`), the same
   tool schemas (`conversation.tools.tool_schemas_for`), the same
   `ToolExecutor` the voice webhook uses. Only the transport differs (text,
   not Vapi). Every tool call is traced as a `call_events` row.
2. **Tool bench.** Runs one tool (`check_availability`, `book_appointment`,
   `answer_faq`, `request_callback`) directly with typed arguments. No model
   is involved, so it works without `ANTHROPIC_API_KEY` and results are
   deterministic.
3. **Day board + activity.** Confirmed appointments per bay for a day, and a
   newest-first feed of tool calls (real and simulated) for live polling.

## What a simulated call does and doesn't do

| | Simulated call |
|---|---|
| Stored as | a `calls` row with `direction = 'simulated'` |
| Books appointments | **yes**, real rows and real calendar events (that is the point) |
| Sends confirmations (email/SMS) | no |
| CRM sync | no (`workers/post_call.py` skips it) |
| Post-call summary + lead score | yes, on **End call** |
| Analytics | excluded (`analytics.service` filters it out) |
| Appointments page | listed, with a **test** badge (`AppointmentSummary.simulated`) |
| **Discard** | deletes the call, its events, its appointments and their calendar events |

## Inputs / outputs

Routes under `/api/v1/test-lab`, all `admin` or `owner` only:

| Method | Path | Does |
|---|---|---|
| GET | `/status` | bays, services, booking fields, timezone, whether the model is configured |
| POST | `/sessions` | starts a simulated call; returns the greeting |
| POST | `/sessions/{id}/messages` | one caller turn; returns the reply + tool trace |
| POST | `/sessions/{id}/tools/{tool}` | tool bench run |
| POST | `/sessions/{id}/end` | ends the call and queues the post-call job |
| DELETE | `/sessions/{id}` | discards everything the session created |
| GET | `/board?day=YYYY-MM-DD` | the day board |
| GET | `/activity?after=&limit=` | the activity feed, incremental |

## Configuration

- `ANTHROPIC_API_KEY` for simulated calls (the bench and board work without
  it; the API answers `503 llm_unavailable` and the page says so).
- Conversation history lives in Redis (`lab:history:{call_id}`, 4 h TTL),
  in Anthropic tool-use format, so a tool round is replayed to the model
  exactly as it happened.

## Dependencies

`tenants` (prompt), `conversation` (tools, LLM provider), `scheduling`
(bays, calendar), `telephony` (call rows), `core.queue` (post-call job).

## Known risks / future improvements

- The simulator tells the model the current local time with the same
  sentence real calls get (`telephony.providers.base.clock_instructions`).
  The lab fills in real values; on a phone call Vapi fills them in per call.
- Text is not speech: the lab does not exercise ASR errors, barge-in, or
  latency the caller would hear. Use a real test call for those.
- At most `MAX_TOOL_ROUNDS` (5) tool rounds per turn; a model stuck calling
  tools gets a fixed apology instead.
- Simulated bookings occupy real calendar slots until discarded or
  cancelled. Run the lab against a test calendar where possible.
