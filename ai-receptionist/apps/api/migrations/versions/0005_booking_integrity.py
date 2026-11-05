"""Booking integrity: make double-booking impossible at the database level,
and booking retries harmless.

Two constraints carry the correctness guarantee from Phase 1 Risk R4:

1. A partial unique index on (tenant_id, starts_at) for *confirmed*
   appointments. Google Calendar has no atomic "create if free" primitive, so
   this index — not the calendar — is the serialization point. Two callers
   racing for the same slot both pass an availability check; only one insert
   survives, and the loser is offered alternatives.
   Cancelled/no-show rows are excluded so a freed slot can be rebooked.

2. `idempotency_key`, unique. A retried webhook (vendors retry) or a repeated
   tool call must return the original booking rather than creating a second.

Contact details move onto the appointment because confirmations are sent from
the async path, after the call has ended, and a lead row may not exist.

Revision ID: 0005
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE appointments ADD COLUMN idempotency_key text")
    op.execute("ALTER TABLE appointments ADD COLUMN customer_name text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE appointments ADD COLUMN customer_phone text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE appointments ADD COLUMN customer_email text")
    op.execute("ALTER TABLE appointments ADD COLUMN timezone text NOT NULL DEFAULT 'UTC'")

    op.execute("""
        CREATE UNIQUE INDEX appointments_idempotency_key_unique
        ON appointments (idempotency_key) WHERE idempotency_key IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX appointments_no_double_booking
        ON appointments (tenant_id, starts_at) WHERE status = 'confirmed'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS appointments_no_double_booking")
    op.execute("DROP INDEX IF EXISTS appointments_idempotency_key_unique")
    for column in (
        "timezone",
        "customer_email",
        "customer_phone",
        "customer_name",
        "idempotency_key",
    ):
        op.execute(f"ALTER TABLE appointments DROP COLUMN IF EXISTS {column}")
