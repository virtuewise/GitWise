"""Add recoverable processing leases to GitHub webhook deliveries."""

from alembic import op

revision = "0003_webhook_delivery_processing"
down_revision = "0002_github_webhook_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE github_webhook_deliveries
        ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (processing_status IN ('pending', 'processing', 'processed', 'failed')),
        ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        ADD COLUMN lease_expires_at TIMESTAMPTZ,
        ADD COLUMN processed_at TIMESTAMPTZ,
        ADD COLUMN last_error_code TEXT,
        ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    """)
    op.execute("""
        CREATE INDEX ix_github_webhook_deliveries_retry
        ON github_webhook_deliveries (processing_status, lease_expires_at, received_at)
        WHERE processing_status IN ('pending', 'failed', 'processing')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_github_webhook_deliveries_retry")
    op.execute("""
        ALTER TABLE github_webhook_deliveries
        DROP COLUMN IF EXISTS updated_at,
        DROP COLUMN IF EXISTS last_error_code,
        DROP COLUMN IF EXISTS processed_at,
        DROP COLUMN IF EXISTS lease_expires_at,
        DROP COLUMN IF EXISTS attempt_count,
        DROP COLUMN IF EXISTS processing_status
    """)
