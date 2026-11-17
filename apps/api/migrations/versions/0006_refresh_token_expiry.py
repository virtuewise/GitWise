"""Persist GitHub refresh-token expiry alongside encrypted user sessions."""

from alembic import op

revision = "0006_refresh_token_expiry"
down_revision = "0005_user_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE user_sessions
        ADD COLUMN refresh_token_expires_at TIMESTAMPTZ
    """)
    op.execute("""
        ALTER TABLE user_sessions
        ADD CONSTRAINT ck_user_sessions_refresh_expiry_pair
        CHECK (
            refresh_token_expires_at IS NULL OR encrypted_refresh_token IS NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE user_sessions
        DROP CONSTRAINT IF EXISTS ck_user_sessions_refresh_expiry_pair
    """)
    op.execute("""
        ALTER TABLE user_sessions
        DROP COLUMN IF EXISTS refresh_token_expires_at
    """)
