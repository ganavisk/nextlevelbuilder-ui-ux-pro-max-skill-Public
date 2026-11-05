# conversation

**Purpose:** The AI brain. Knowledge-base ingestion + RAG retrieval, grounded
FAQ answering, deterministic lead qualification scoring, post-call
summarization, and the in-call tool executor that ties them together.

**Dependencies:** `core` (db, config, logging), `escalation` (callback
capture from the executor), `tenants` (agent config type). Provider clients
(Claude, Voyage) live behind `providers/` interfaces.

**Inputs:** caller questions and tool-call arguments (via `tools.ToolExecutor`,
called from the telephony webhook route); transcripts (post-call worker).
**Outputs:** spoken-language tool results; `leads` rows with merged
qualification + score; `knowledge_chunks` embeddings; call
outcome/sentiment/summary/cost.

**Configuration:** `ANTHROPIC_API_KEY`, `LLM_MODEL_PRIMARY` (Sonnet, in-call),
`LLM_MODEL_FAST` (Haiku, classification/summaries), `VOYAGE_API_KEY`,
`EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`.

**Design rules:**
- **FAQ is grounded-only** — no relevant chunk (cosine distance beyond the
  confidence floor), an empty/refused completion, or any failure all route to
  the same safe fallback. This is the Risk R3 hallucination guardrail; it
  short-circuits *before* the LLM when retrieval finds nothing.
- **Qualification does no LLM call** — Claude conducts the conversation via
  the vendor system prompt; this module scores and persists deterministically,
  upserting atomically per (tenant_id, call_id).
- **Model tiers:** in-call path disables thinking + low effort (latency);
  fast path omits thinking/effort entirely (Haiku rejects them).
- **Cost metering is best-effort** — a metering failure never breaks the
  caller's turn.

**Future improvements:** sentence/paragraph-aware chunking; reranking;
appointment-booking tool (Phase 5); conversation eval suite as a CI gate.

**Known risks:** Voyage adapter and the Vapi tool-call/response shapes are
covered by fakes/fixtures but not yet contract-validated against live
sandboxes (M1 live-call test); the naive fixed-window chunker is fine for
FAQ content but weak for long-form documents.
