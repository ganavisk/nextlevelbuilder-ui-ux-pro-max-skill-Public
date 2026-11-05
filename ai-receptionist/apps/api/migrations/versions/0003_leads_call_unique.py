"""Prevent duplicate leads per call under concurrent qualification writes.

Multiple record_qualification_answer tool calls can arrive in quick
succession within one call; the conversation module upserts on this
constraint so they always converge on a single lead row instead of racing
to create duplicates.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX leads_tenant_call_unique
        ON leads (tenant_id, call_id) WHERE call_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS leads_tenant_call_unique")
