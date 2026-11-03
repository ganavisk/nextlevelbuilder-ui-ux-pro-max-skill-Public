"""Service bays: let a business run several appointments at once, and make
overlapping bookings impossible rather than only same-start ones.

1. `appointments.bay` — which of the tenant's parallel resources (an auto
   shop's service bays; a single-chair practice just has bay 1) an
   appointment occupies.

2. The double-booking guard becomes an exclusion constraint: no two confirmed
   appointments for the same tenant *and bay* may overlap in time. The old
   unique index on (tenant_id, starts_at) only stopped two bookings starting at
   the same instant — a 9:00 hour-long booking and a 9:30 one could both win a
   race — and it made more than one booking per start time impossible, which a
   three-bay shop needs constantly. Bay 1 everywhere keeps existing tenants'
   behaviour, now with true overlap protection. Needs btree_gist (for the
   equality parts of a GiST constraint); it is a trusted extension, so the
   non-superuser application role can create it.

   NOTE: on a database that already holds overlapping confirmed
   appointments this migration fails rather than silently keeping them —
   resolve those rows first (they are double bookings).

3. `appointments.details` — what the caller told us that the business asked
   to collect at booking time (`scheduling.booking_fields`, e.g. vehicle and
   mileage for an auto shop), so staff see it next to the appointment.

4. Vertical `auto_repair`, and call direction `simulated` for test-lab calls
   (kept out of analytics, CRM sync and real confirmations).

Revision ID: 0008
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.execute(
        "ALTER TABLE appointments ADD COLUMN bay smallint NOT NULL DEFAULT 1 CHECK (bay >= 1)"
    )
    op.execute("ALTER TABLE appointments ADD COLUMN details jsonb NOT NULL DEFAULT '{}'::jsonb")

    op.execute("DROP INDEX IF EXISTS appointments_no_double_booking")
    op.execute("""
        ALTER TABLE appointments ADD CONSTRAINT appointments_no_overlap_per_bay
        EXCLUDE USING gist (
            tenant_id WITH =,
            bay WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        ) WHERE (status = 'confirmed')
    """)

    op.execute("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_vertical_check")
    op.execute("""
        ALTER TABLE tenants ADD CONSTRAINT tenants_vertical_check
        CHECK (vertical IN ('dental', 'legal', 'auto_repair', 'other'))
    """)

    op.execute("ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_direction_check")
    op.execute("""
        ALTER TABLE calls ADD CONSTRAINT calls_direction_check
        CHECK (direction IN ('inbound', 'outbound', 'simulated'))
    """)


def downgrade() -> None:
    op.execute("DELETE FROM calls WHERE direction = 'simulated'")
    op.execute("ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_direction_check")
    op.execute("""
        ALTER TABLE calls ADD CONSTRAINT calls_direction_check
        CHECK (direction IN ('inbound', 'outbound'))
    """)
    op.execute("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_vertical_check")
    op.execute("""
        ALTER TABLE tenants ADD CONSTRAINT tenants_vertical_check
        CHECK (vertical IN ('dental', 'legal', 'other'))
    """)
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_no_overlap_per_bay")
    # Only valid if no two confirmed appointments share a start time any more.
    op.execute("""
        CREATE UNIQUE INDEX appointments_no_double_booking
        ON appointments (tenant_id, starts_at) WHERE status = 'confirmed'
    """)
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS details")
    op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS bay")
