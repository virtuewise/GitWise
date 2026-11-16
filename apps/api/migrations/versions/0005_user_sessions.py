"""Create one-time OAuth state and server-managed user session tables."""

from alembic import op

revision = "0005_user_sessions"
down_revision = "0004_webhook_claim_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE oauth_authorization_states (
            state_hash BYTEA PRIMARY KEY CHECK (octet_length(state_hash) = 32),
            encrypted_code_verifier BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            CHECK (expires_at > created_at)
        )
    """)
    op.execute("""
        CREATE INDEX ix_oauth_authorization_states_expires_at
        ON oauth_authorization_states (expires_at)
    """)
    op.execute("""
        CREATE TABLE user_sessions (
            session_id UUID PRIMARY KEY,
            token_hash BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
            csrf_hash BYTEA NOT NULL CHECK (octet_length(csrf_hash) = 32),
            github_user_id BIGINT NOT NULL CHECK (github_user_id > 0),
            github_login TEXT NOT NULL CHECK (length(github_login) BETWEEN 1 AND 255),
            encrypted_access_token BYTEA NOT NULL,
            encrypted_refresh_token BYTEA,
            access_token_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            idle_expires_at TIMESTAMPTZ NOT NULL,
            absolute_expires_at TIMESTAMPTZ NOT NULL,
            rotated_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CHECK (absolute_expires_at > created_at),
            CHECK (idle_expires_at <= absolute_expires_at)
        )
    """)
    op.execute("""
        CREATE INDEX ix_user_sessions_active_user
        ON user_sessions (github_user_id)
        WHERE revoked_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_sessions")
    op.execute("DROP TABLE IF EXISTS oauth_authorization_states")
