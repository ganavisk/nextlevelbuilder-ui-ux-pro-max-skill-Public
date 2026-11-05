"""CRM sync ledger: make a retried sync resume rather than repeat.

A CRM sync is not one write, it is three against a vendor with no idempotency
key of its own: upsert the contact, log the call activity, create the deal.
If the deal write fails after the first two succeeded, retrying the whole job
would log a second activity and — once the deal step worked — could leave a
duplicate deal in the customer's CRM. Duplicates in someone's CRM are worse
than a missing record: they are noticed, and they have to be merged by hand.

So each external id is recorded the moment it is obtained, and the retry
skips the steps that already have one. This table is the resume point.

It also satisfies NFR-06 (auditability): every CRM side effect is traceable
back to the call that caused it, with the attempt count and last error that
produced it.

`idempotency_key` is unique, so two workers processing the same job converge
on one ledger row instead of syncing the lead twice — but converging on the
same row is not enough by itself. The queue's own delivery guarantee is
at-least-once (see core/queue.py): a worker presumed dead can be reclaimed
while it is still alive, and the reclaimed job then runs concurrently with
its "zombie" original. Without a further guard, both would see the same
NULL columns and both write — two contacts, or worse, two deals.

`status='in_progress'` plus `claimed_at` closes that gap: an attempt must
win an atomic UPDATE that only succeeds against `pending`/`failed`, or
against a stale `in_progress` (a claim older than CLAIM_STALE_AFTER — the
prior holder is presumed dead, not merely slow). The loser does no work at
all rather than racing on individual steps.

Revision ID: 0006
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE crm_syncs (
        id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        lead_id               uuid REFERENCES leads(id) ON DELETE SET NULL,
        call_id               uuid REFERENCES calls(id) ON DELETE SET NULL,
        appointment_id        uuid REFERENCES appointments(id) ON DELETE SET NULL,
        provider              text NOT NULL DEFAULT 'hubspot',
        -- External ids double as per-step completion markers: non-null means
        -- that step is done and must not be repeated on retry.
        contact_external_id   text,
        activity_external_id  text,
        deal_external_id      text,
        status                text NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','in_progress','synced','failed','dead')),
        attempts              integer NOT NULL DEFAULT 0,
        last_error            text,
        idempotency_key       text NOT NULL,
        created_at            timestamptz NOT NULL DEFAULT now(),
        -- When the current attempt claimed the row. Distinguishes a live
        -- in-flight attempt from an abandoned one — see module docstring.
        claimed_at            timestamptz,
        synced_at             timestamptz
    )""")

    op.execute(
        "CREATE UNIQUE INDEX crm_syncs_idempotency_key_unique ON crm_syncs (idempotency_key)"
    )
    # The dashboard's "needs attention" view, and the operator's first
    # question after an outage: what failed and hasn't recovered?
    op.execute(
        "CREATE INDEX crm_syncs_tenant_status_idx ON crm_syncs (tenant_id, status, created_at DESC)"
    )

    op.execute("ALTER TABLE crm_syncs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE crm_syncs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON crm_syncs
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS crm_syncs")
