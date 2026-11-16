import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from gitwise_api.genome import GenomeFact, SoftwareGenomeSnapshot
from gitwise_api.storage import (
    GenomeSnapshotStore,
    SnapshotAccessDenied,
    SnapshotContentConflict,
)

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")


def test_snapshot_store_requires_async_postgresql_url() -> None:
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        GenomeSnapshotStore.from_url("sqlite+aiosqlite:///:memory:")


@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration database is not configured")
def test_postgresql_snapshot_authorization_idempotency_conflict_and_immutability() -> None:
    async def exercise() -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        repository_id = uuid4().int % (2**62) + 1
        principal_id = f"test-user-{uuid4()}"
        repository = f"gitwise-integration/repo-{repository_id}"
        repository_url = f"https://github.com/{repository}"
        commit_sha = "a" * 40
        snapshot = SoftwareGenomeSnapshot(
            repository=repository,
            repository_url=repository_url,
            provider_repository_id=repository_id,
            default_branch="main",
            commit_sha=commit_sha,
            tree_sha="b" * 40,
            is_private=False,
            generated_at=datetime.now(UTC),
        )
        store = GenomeSnapshotStore.from_url(DATABASE_URL)
        try:
            with pytest.raises(SnapshotAccessDenied):
                await store.store_snapshot(
                    snapshot, principal_type="github_user", principal_id=principal_id
                )

            async with store._engine.begin() as connection:
                await connection.execute(
                    text("""
                        INSERT INTO provider_repositories (
                            provider, provider_repository_id, owner_login,
                            repository_name, canonical_url, is_private
                        ) VALUES ('github', :id, 'gitwise-integration', :name, :url, false)
                    """),
                    {"id": repository_id, "name": f"repo-{repository_id}", "url": repository_url},
                )
                await connection.execute(
                    text("""
                        INSERT INTO repository_access_grants (
                            grant_id, provider, provider_repository_id, principal_type,
                            principal_id, permission, granted_by
                        ) VALUES (
                            :grant_id, 'github', :id, 'github_user',
                            :principal, 'write', 'integration-test'
                        )
                    """),
                    {"grant_id": str(uuid4()), "id": repository_id, "principal": principal_id},
                )

            first = await store.store_snapshot(
                snapshot, principal_type="github_user", principal_id=principal_id
            )
            second = await store.store_snapshot(
                snapshot, principal_type="github_user", principal_id=principal_id
            )
            assert first.outcome == "created"
            assert second.outcome == "already_exists"
            assert first.snapshot_key == second.snapshot_key

            changed = snapshot.model_copy(deep=True)
            changed.facts.append(GenomeFact.observed(
                repository=repository,
                commit_sha=commit_sha,
                fact_type="project.configuration",
                value={"setting": "changed"},
                source_path="pyproject.toml",
                extraction_method="integration_test",
            ))
            with pytest.raises(SnapshotContentConflict):
                await store.store_snapshot(
                    changed, principal_type="github_user", principal_id=principal_id
                )

            with pytest.raises(DBAPIError):
                async with store._engine.begin() as connection:
                    await connection.execute(
                        text("""
                            UPDATE software_genome_snapshots
                            SET content_digest = :digest
                            WHERE snapshot_key = :key
                        """),
                        {"digest": "0" * 64, "key": first.snapshot_key},
                    )
        finally:
            async with store._engine.begin() as connection:
                await connection.execute(
                    text("""
                        DELETE FROM provider_repositories
                        WHERE provider = 'github' AND provider_repository_id = :id
                    """),
                    {"id": repository_id},
                )
            await store.close()

    asyncio.run(exercise())
