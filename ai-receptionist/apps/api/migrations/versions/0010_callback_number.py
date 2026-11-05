"""A callback number separate from the caller ID.

`callback_requests.caller_e164` is the number the call came from. A caller on
a work line, a borrowed phone, or with caller ID blocked can now ask to be
called back somewhere else; that number goes in `callback_e164`, and the
dashboard's callback queue dials it in preference to the caller ID. Null
means "call the number they called from", which is every existing row.

Revision ID: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("callback_requests", sa.Column("callback_e164", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("callback_requests", "callback_e164")
