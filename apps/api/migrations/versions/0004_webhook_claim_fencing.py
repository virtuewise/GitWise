"""Fence stale webhook workers from completing reclaimed deliveries."""

from alembic import op

revision = "0004_webhook_claim_fencing"
down_revision = "0003_webhook_delivery_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE github_webhook_deliveries
        ADD COLUMN claim_token UUID
    """)
    op.execute("""
        ALTER TABLE github_webhook_deliveries
        ADD CONSTRAINT ck_webhook_processing_claim_token
        CHECK (
            (processing_status = 'processing' AND claim_token IS NOT NULL)
            OR (processing_status <> 'processing' AND claim_token IS NULL)
        )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE github_webhook_deliveries
        DROP CONSTRAINT IF EXISTS ck_webhook_processing_claim_token
    """)
    op.execute("""
        ALTER TABLE github_webhook_deliveries
        DROP COLUMN IF EXISTS claim_token
    """)
