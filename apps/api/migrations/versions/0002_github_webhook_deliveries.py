"""Persist GitHub webhook delivery IDs to prevent duplicate event processing."""

from alembic import op

revision = "0002_github_webhook_deliveries"
down_revision = "0001_genome_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE github_webhook_deliveries (
            delivery_id UUID PRIMARY KEY,
            event_name TEXT NOT NULL CHECK (length(event_name) BETWEEN 1 AND 100),
            payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_github_webhook_deliveries_received_at
        ON github_webhook_deliveries (received_at)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS github_webhook_deliveries")
