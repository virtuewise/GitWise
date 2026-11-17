"""Erase recoverable GitHub credentials from sessions revoked by older code."""

from alembic import op

revision = "0007_sanitize_revoked_sessions"
down_revision = "0006_refresh_token_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier logout handlers only set revoked_at, leaving encrypted provider
    # credentials recoverable in the session row. This migration is deliberately
    # irreversible: credential erasure must not be undone by a schema downgrade.
    op.execute(
        """
        UPDATE user_sessions
        SET encrypted_access_token = convert_to('revoked-session', 'UTF8'),
            encrypted_refresh_token = NULL,
            access_token_expires_at = NULL,
            refresh_token_expires_at = NULL
        WHERE revoked_at IS NOT NULL
        """
    )


def downgrade() -> None:
    # Intentionally retain credential erasure on downgrade.
    pass
