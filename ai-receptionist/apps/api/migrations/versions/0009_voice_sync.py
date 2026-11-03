"""Track whether the voice vendor's agent reflects the tenant's settings.

A dashboard save that changes what the agent is told (prompt, greeting,
voice, timezone, booking fields...) now queues a push to the voice vendor.
These columns let the dashboard say truthfully whether that push has landed:

- `voice_sync_requested`: bumped on every save that changes the agent.
- `voice_sync_done`: the highest requested version known to be live. A job
  records the version it *read* before building the agent, so a save that
  lands mid-push stays "updating" until a later job covers it.
- `voice_sync_failed_version` / `voice_sync_error`: the last version that
  could not be pushed, and why (shown with a retry button).
- `voice_synced_at`: when the last successful push finished.

Existing tenants start at 0/0, i.e. "live" if provisioned. That's accurate
as long as `make provision` was run after their last change, which was the
only way to change a live agent before this migration.

Revision ID: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("voice_sync_requested", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tenants", sa.Column("voice_sync_done", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "tenants",
        sa.Column("voice_sync_failed_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tenants", sa.Column("voice_sync_error", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        "tenants", sa.Column("voice_synced_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    for column in (
        "voice_synced_at",
        "voice_sync_error",
        "voice_sync_failed_version",
        "voice_sync_done",
        "voice_sync_requested",
    ):
        op.drop_column("tenants", column)
