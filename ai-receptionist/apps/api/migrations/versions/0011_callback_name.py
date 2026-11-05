"""Who to ask for when returning a call.

The agent was told to collect the caller's name for a callback, but
`request_callback` had nowhere to put it: the name survived only if the
model happened to write it into the free-text reason. `caller_name` gives it
a column the callback queue can show. Empty when the caller didn't give one
(existing rows, and callers who'd rather not say); a callback is never
refused for want of a name.

Revision ID: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "callback_requests",
        sa.Column("caller_name", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("callback_requests", "caller_name")
