"""Let tenants choose the TTS voice vendor and model, not just a voice id.

`voice_id` alone is ambiguous — the same string means nothing without knowing
which vendor's library it comes from. Adding the provider lets a tenant use
ElevenLabs voices while the voice-agent orchestration stays with Vapi.

`voice_model` matters for the latency budget (NFR-01): ElevenLabs' turbo/flash
models are materially faster than the standard multilingual model, which is
the difference between a natural and a laggy-feeling receptionist.

Both default to '' meaning "vendor default", so existing rows keep working.

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_configs ADD COLUMN voice_provider text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE agent_configs ADD COLUMN voice_model text NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_configs DROP COLUMN IF EXISTS voice_model")
    op.execute("ALTER TABLE agent_configs DROP COLUMN IF EXISTS voice_provider")
