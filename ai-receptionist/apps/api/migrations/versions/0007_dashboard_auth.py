"""Dashboard auth: password login for the users table.

Login resolves a tenant by its (public, RLS-free) `slug` before any tenant
context exists — mirroring how `resolve_tenant_by_number` resolves a tenant
from a dialed number in migration 0002 — then runs the user lookup inside
`tenant_session(tenant_id)`, which the existing `tenant_isolation` policy on
`users` already permits. No new RLS policy is needed for login itself.

`NOT NULL DEFAULT ''` rather than nullable: an empty hash can never verify
against any password (bcrypt would raise on a malformed hash before it could
accidentally succeed), so a user row created without a password is
unusable-but-safe rather than nullable-and-forgettable.

Revision ID: 0007
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN password_hash text NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS password_hash")
