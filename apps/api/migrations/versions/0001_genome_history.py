"""Create provider repository, revocable access grant, and immutable Genome snapshot tables."""

from alembic import op

revision = "0001_genome_history"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE provider_repositories (
            provider TEXT NOT NULL,
            provider_repository_id BIGINT NOT NULL CHECK (provider_repository_id > 0),
            owner_login TEXT NOT NULL,
            repository_name TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            is_private BOOLEAN NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, provider_repository_id),
            CHECK (provider = 'github')
        )
    """)
    op.execute("""
        CREATE TABLE repository_access_grants (
            grant_id UUID PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_repository_id BIGINT NOT NULL,
            principal_type TEXT NOT NULL
                CHECK (principal_type IN ('github_user', 'github_app_installation')),
            principal_id TEXT NOT NULL CHECK (length(principal_id) BETWEEN 1 AND 255),
            permission TEXT NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at TIMESTAMPTZ,
            granted_by TEXT NOT NULL,
            FOREIGN KEY (provider, provider_repository_id)
                REFERENCES provider_repositories(provider, provider_repository_id)
                ON DELETE CASCADE,
            CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_active_repository_principal_grant
        ON repository_access_grants (
            provider, provider_repository_id, principal_type, principal_id
        )
        WHERE revoked_at IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_active_grants_principal
        ON repository_access_grants (principal_type, principal_id, provider, provider_repository_id)
        WHERE revoked_at IS NULL
    """)
    op.execute("""
        CREATE TABLE software_genome_snapshots (
            snapshot_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_repository_id BIGINT NOT NULL,
            commit_sha TEXT NOT NULL CHECK (commit_sha ~ '^[0-9a-fA-F]{40,64}$'),
            schema_version TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 32),
            content_digest CHAR(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
            payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            observed_at TIMESTAMPTZ NOT NULL,
            stored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (provider, provider_repository_id)
                REFERENCES provider_repositories(provider, provider_repository_id)
                ON DELETE CASCADE,
            UNIQUE (provider, provider_repository_id, commit_sha, schema_version)
        )
    """)
    op.execute("""
        CREATE INDEX ix_genome_snapshots_repository_history
        ON software_genome_snapshots (provider, provider_repository_id, stored_at DESC)
    """)
    op.execute("""
        CREATE FUNCTION reject_genome_snapshot_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Software Genome snapshots are immutable';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_genome_snapshots_immutable
        BEFORE UPDATE ON software_genome_snapshots
        FOR EACH ROW EXECUTE FUNCTION reject_genome_snapshot_update()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_genome_snapshots_immutable ON software_genome_snapshots")
    op.execute("DROP FUNCTION IF EXISTS reject_genome_snapshot_update()")
    op.execute("DROP TABLE IF EXISTS software_genome_snapshots")
    op.execute("DROP TABLE IF EXISTS repository_access_grants")
    op.execute("DROP TABLE IF EXISTS provider_repositories")
