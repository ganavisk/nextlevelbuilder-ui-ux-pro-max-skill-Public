"""Allow phone-number → tenant resolution before tenant context exists.

Inbound webhooks arrive knowing only the dialed number; the tenant is not yet
known, so `app.tenant_id` is unset. This SELECT-only policy on phone_numbers
permits that one system lookup. Policies are permissive (OR'd), so
tenant-scoped sessions are unaffected, and no other table gains any access.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULLIF(...,'') treats a leftover empty string (see migration 0001's
    # comment on the pooled-connection reset edge case) the same as a truly
    # unset variable — both mean "no tenant scope", so the lookup is allowed.
    op.execute("""
        CREATE POLICY system_number_lookup ON phone_numbers
        FOR SELECT
        USING (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS system_number_lookup ON phone_numbers")
