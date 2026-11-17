"""Index terminal user sessions for bounded retention purges."""

from alembic import op

revision = "0008_session_retention_indexes"
down_revision = "0007_sanitize_revoked_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX ix_user_sessions_retention_revoked
        ON user_sessions (revoked_at, session_id)
        WHERE revoked_at IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_user_sessions_retention_expired
        ON user_sessions (
            LEAST(idle_expires_at, absolute_expires_at),
            session_id
        )
        WHERE revoked_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_sessions_retention_expired")
    op.execute("DROP INDEX IF EXISTS ix_user_sessions_retention_revoked")
