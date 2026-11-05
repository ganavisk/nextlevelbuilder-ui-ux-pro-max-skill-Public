"""Initial schema: tenants, calls, knowledge base, scheduling, CRM sync,
notifications, and the tool-invocation audit spine — with row-level security
on every tenant-scoped table.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Every table listed here gets: a tenant_id FK, RLS enabled, and a policy
# restricting rows to current_setting('app.tenant_id'). The application
# connects as a non-superuser role so the policies actually bite.
TENANT_TABLES = [
    "users",
    "phone_numbers",
    "agent_configs",
    "knowledge_docs",
    "knowledge_chunks",
    "integrations",
    "calls",
    "transcripts",
    "call_events",
    "tool_invocations",
    "leads",
    "appointments",
    "callback_requests",
    "notifications",
]


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')  # gen_random_uuid
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
    CREATE TABLE tenants (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        slug            text NOT NULL UNIQUE,
        name            text NOT NULL,
        vertical        text NOT NULL CHECK (vertical IN ('dental', 'legal', 'other')),
        timezone        text NOT NULL DEFAULT 'America/New_York',
        business_hours  jsonb NOT NULL DEFAULT '{}',   -- {"mon":[["09:00","17:00"]],...}
        settings        jsonb NOT NULL DEFAULT '{}',   -- consent line, forbidden topics, caps
        status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE users (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        email         text NOT NULL,
        display_name  text NOT NULL DEFAULT '',
        role          text NOT NULL DEFAULT 'viewer' CHECK (role IN ('owner','admin','viewer')),
        created_at    timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, email)
    )""")

    op.execute("""
    CREATE TABLE phone_numbers (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        e164             text NOT NULL UNIQUE,          -- inbound number, resolves tenant
        vendor           text NOT NULL DEFAULT 'vapi',
        vendor_number_id text NOT NULL DEFAULT '',
        vendor_agent_id  text NOT NULL DEFAULT '',
        created_at       timestamptz NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE agent_configs (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        version           integer NOT NULL,
        is_active         boolean NOT NULL DEFAULT false,
        system_prompt     text NOT NULL,
        first_message     text NOT NULL,
        voice_id          text NOT NULL DEFAULT '',
        language          text NOT NULL DEFAULT 'en',
        qualification     jsonb NOT NULL DEFAULT '[]',  -- ordered questions + scoring rules
        escalation_policy jsonb NOT NULL DEFAULT '{"mode":"voicemail_callback"}',
        created_at        timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, version)
    )""")
    op.execute("""
    CREATE UNIQUE INDEX one_active_config_per_tenant
        ON agent_configs (tenant_id) WHERE is_active
    """)

    op.execute("""
    CREATE TABLE knowledge_docs (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        title        text NOT NULL,
        source_type  text NOT NULL DEFAULT 'manual' CHECK (source_type IN ('manual','upload','url')),
        content      text NOT NULL,
        status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','indexed','failed')),
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE knowledge_chunks (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        doc_id       uuid NOT NULL REFERENCES knowledge_docs(id) ON DELETE CASCADE,
        chunk_index  integer NOT NULL,
        content      text NOT NULL,
        embedding    vector(1024),
        UNIQUE (doc_id, chunk_index)
    )""")
    op.execute("""
    CREATE INDEX knowledge_chunks_embedding_idx ON knowledge_chunks
        USING hnsw (embedding vector_cosine_ops)
    """)

    op.execute("""
    CREATE TABLE integrations (
        id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        provider              text NOT NULL CHECK (provider IN ('google_calendar','hubspot')),
        credentials_encrypted text NOT NULL,             -- Fernet ciphertext, key in env only
        config                jsonb NOT NULL DEFAULT '{}',
        status                text NOT NULL DEFAULT 'connected'
                              CHECK (status IN ('connected','error','revoked')),
        created_at            timestamptz NOT NULL DEFAULT now(),
        updated_at            timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, provider)
    )""")

    op.execute("""
    CREATE TABLE calls (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        phone_number_id  uuid REFERENCES phone_numbers(id),
        vendor_call_id   text NOT NULL,
        caller_e164      text NOT NULL,
        direction        text NOT NULL DEFAULT 'inbound' CHECK (direction IN ('inbound','outbound')),
        started_at       timestamptz NOT NULL DEFAULT now(),
        ended_at         timestamptz,
        outcome          text CHECK (outcome IN
                         ('answered_faq','lead_qualified','appointment_booked',
                          'callback_requested','voicemail','abandoned','other')),
        summary          text,
        sentiment        text CHECK (sentiment IN ('positive','neutral','negative')),
        recording_url    text,
        vendor_cost_cents integer NOT NULL DEFAULT 0,
        llm_cost_cents    integer NOT NULL DEFAULT 0,
        created_at       timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, vendor_call_id)
    )""")
    op.execute("CREATE INDEX calls_tenant_started_idx ON calls (tenant_id, started_at DESC)")

    op.execute("""
    CREATE TABLE transcripts (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        call_id    uuid NOT NULL UNIQUE REFERENCES calls(id) ON DELETE CASCADE,
        turns      jsonb NOT NULL DEFAULT '[]',  -- [{"role","text","ts"}]
        created_at timestamptz NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE call_events (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        call_id     uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
        event_type  text NOT NULL,
        payload     jsonb NOT NULL DEFAULT '{}',
        occurred_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX call_events_call_idx ON call_events (call_id, occurred_at)")

    # The audit spine: every side effect (booking, CRM write, SMS) is recorded
    # here with request/response and an idempotency key (NFR-06, NFR-08).
    op.execute("""
    CREATE TABLE tool_invocations (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        call_id         uuid REFERENCES calls(id) ON DELETE SET NULL,
        tool_name       text NOT NULL,
        idempotency_key text NOT NULL UNIQUE,
        request         jsonb NOT NULL DEFAULT '{}',
        response        jsonb NOT NULL DEFAULT '{}',
        status          text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','succeeded','failed','retrying')),
        error           text,
        started_at      timestamptz NOT NULL DEFAULT now(),
        finished_at     timestamptz
    )""")
    op.execute("CREATE INDEX tool_invocations_call_idx ON tool_invocations (call_id)")

    op.execute("""
    CREATE TABLE leads (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        call_id       uuid REFERENCES calls(id) ON DELETE SET NULL,
        name          text,
        phone         text NOT NULL,
        email         text,
        score         integer NOT NULL DEFAULT 0,
        qualification jsonb NOT NULL DEFAULT '{}',  -- answers to qualification questions
        status        text NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new','qualified','unqualified','converted')),
        crm_contact_id text,
        crm_synced_at  timestamptz,
        created_at    timestamptz NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE appointments (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        lead_id           uuid REFERENCES leads(id) ON DELETE SET NULL,
        call_id           uuid REFERENCES calls(id) ON DELETE SET NULL,
        service           text NOT NULL DEFAULT '',
        starts_at         timestamptz NOT NULL,
        ends_at           timestamptz NOT NULL,
        external_event_id text NOT NULL DEFAULT '',   -- calendar is source of truth
        status            text NOT NULL DEFAULT 'confirmed'
                          CHECK (status IN ('confirmed','cancelled','completed','no_show')),
        created_at        timestamptz NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE callback_requests (
        id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        call_id              uuid REFERENCES calls(id) ON DELETE SET NULL,
        caller_e164          text NOT NULL,
        reason               text NOT NULL DEFAULT '',
        voicemail_transcript text,
        preferred_window     text,
        status               text NOT NULL DEFAULT 'open'
                             CHECK (status IN ('open','contacted','resolved')),
        created_at           timestamptz NOT NULL DEFAULT now()
    )""")

    op.execute("""
    CREATE TABLE notifications (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        appointment_id      uuid REFERENCES appointments(id) ON DELETE SET NULL,
        call_id             uuid REFERENCES calls(id) ON DELETE SET NULL,
        channel             text NOT NULL CHECK (channel IN ('email','sms')),
        recipient           text NOT NULL,
        template            text NOT NULL,
        idempotency_key     text NOT NULL UNIQUE,
        provider_message_id text,
        status              text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','sent','failed')),
        sent_at             timestamptz,
        created_at          timestamptz NOT NULL DEFAULT now()
    )""")

    # Row-level security: tenant-scoped tables are filtered by app.tenant_id.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # missing_ok=true: an unset variable yields NULL (row invisible)
        # instead of erroring, so unscoped sessions simply see nothing.
        # NULLIF(...,'') additionally guards a pooled-connection edge case:
        # once a custom GUC like app.tenant_id has been SET at all on a
        # physical connection (even via SET LOCAL, even in an earlier,
        # already-committed transaction), Postgres's placeholder-reset
        # semantics can leave current_setting() returning '' rather than a
        # true NULL on a later reuse of that connection for an unscoped
        # session — which would otherwise crash the ::uuid cast instead of
        # failing closed.
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")
