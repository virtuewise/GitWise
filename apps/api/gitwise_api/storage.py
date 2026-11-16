"""Transactional PostgreSQL persistence for immutable Software Genome snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gitwise_api.genome import SoftwareGenomeSnapshot
from gitwise_api.snapshot_identity import snapshot_content_digest, snapshot_storage_key

PrincipalType = Literal["github_user", "github_app_installation"]


class SnapshotAccessDenied(PermissionError):
    """Raised when the caller has no active grant for a repository."""


class SnapshotContentConflict(RuntimeError):
    """Raised when an immutable snapshot key already exists with different content."""


@dataclass(frozen=True)
class SnapshotWriteResult:
    snapshot_key: str
    content_digest: str
    outcome: Literal["created", "already_exists"]


class GenomeSnapshotStore:
    """Persist immutable snapshots and enforce active repository grants on access.

    Grant provisioning is deliberately outside this class: only a verified
    GitHub identity/installation flow may create or revoke access grants.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> GenomeSnapshotStore:
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("GITWISE_DATABASE_URL must use postgresql+asyncpg.")
        return cls(create_async_engine(database_url, pool_pre_ping=True))

    async def close(self) -> None:
        await self._engine.dispose()

    async def get_snapshot(
        self,
        snapshot_key: str,
        *,
        principal_type: PrincipalType,
        principal_id: str,
    ) -> SoftwareGenomeSnapshot:
        """Read a snapshot only through the caller's active repository grant.

        Missing snapshots and unauthorized snapshots intentionally produce the
        same error so callers cannot probe private repository history.
        """
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 255:
            raise ValueError("A valid authenticated principal ID is required.")
        if principal_type not in ("github_user", "github_app_installation"):
            raise ValueError("Unsupported repository access principal type.")
        if not isinstance(snapshot_key, str) or not snapshot_key or len(snapshot_key) > 512:
            raise ValueError("A valid snapshot key is required.")

        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("""
                    SELECT snapshot.payload
                    FROM software_genome_snapshots AS snapshot
                    JOIN repository_access_grants AS access_grant
                      ON access_grant.provider = snapshot.provider
                     AND access_grant.provider_repository_id = snapshot.provider_repository_id
                    WHERE snapshot.snapshot_key = :snapshot_key
                      AND access_grant.principal_type = :principal_type
                      AND access_grant.principal_id = :principal_id
                      AND access_grant.revoked_at IS NULL
                      AND access_grant.permission IN ('read', 'write', 'admin')
                    FOR SHARE OF access_grant
                """),
                {
                    "snapshot_key": snapshot_key,
                    "principal_type": principal_type,
                    "principal_id": principal_id,
                },
            )
            payload = result.scalar_one_or_none()
            if payload is None:
                raise SnapshotAccessDenied(
                    "No accessible Software Genome snapshot exists for this principal."
                )
            return SoftwareGenomeSnapshot.model_validate(payload)

    async def store_snapshot(
        self,
        snapshot: SoftwareGenomeSnapshot,
        *,
        principal_type: PrincipalType,
        principal_id: str,
    ) -> SnapshotWriteResult:
        if not principal_id or len(principal_id) > 255:
            raise ValueError("A valid authenticated principal ID is required.")
        if principal_type not in ("github_user", "github_app_installation"):
            raise ValueError("Unsupported repository access principal type.")

        snapshot_key = snapshot_storage_key(snapshot)
        digest = snapshot_content_digest(snapshot)
        owner, separator, name = snapshot.repository.partition("/")
        if not separator or not owner or not name:
            raise ValueError("Snapshot repository must use owner/name format.")

        payload = snapshot.model_dump_json()
        async with self._engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO provider_repositories (
                        provider, provider_repository_id, owner_login,
                        repository_name, canonical_url, is_private
                    ) VALUES (
                        'github', :repository_id, :owner, :name, :url, :is_private
                    )
                    ON CONFLICT (provider, provider_repository_id) DO UPDATE SET
                        owner_login = EXCLUDED.owner_login,
                        repository_name = EXCLUDED.repository_name,
                        canonical_url = EXCLUDED.canonical_url,
                        is_private = EXCLUDED.is_private,
                        updated_at = now()
                """),
                {
                    "repository_id": snapshot.provider_repository_id,
                    "owner": owner,
                    "name": name,
                    "url": snapshot.repository_url,
                    "is_private": snapshot.is_private,
                },
            )

            grant = await connection.execute(
                text("""
                    SELECT permission
                    FROM repository_access_grants
                    WHERE provider = 'github'
                      AND provider_repository_id = :repository_id
                      AND principal_type = :principal_type
                      AND principal_id = :principal_id
                      AND revoked_at IS NULL
                    FOR SHARE
                """),
                {
                    "repository_id": snapshot.provider_repository_id,
                    "principal_type": principal_type,
                    "principal_id": principal_id,
                },
            )
            permission = grant.scalar_one_or_none()
            if permission not in ("write", "admin"):
                raise SnapshotAccessDenied(
                    "An active repository write grant is required to store this snapshot."
                )

            inserted = await connection.execute(
                text("""
                    INSERT INTO software_genome_snapshots (
                        snapshot_key, provider, provider_repository_id, commit_sha,
                        schema_version, content_digest, payload, observed_at
                    ) VALUES (
                        :snapshot_key, 'github', :repository_id, :commit_sha,
                        :schema_version, :digest, CAST(:payload AS JSONB), :observed_at
                    )
                    ON CONFLICT (snapshot_key) DO NOTHING
                    RETURNING snapshot_key
                """),
                {
                    "snapshot_key": snapshot_key,
                    "repository_id": snapshot.provider_repository_id,
                    "commit_sha": snapshot.commit_sha.lower(),
                    "schema_version": snapshot.schema_version,
                    "digest": digest,
                    "payload": payload,
                    "observed_at": snapshot.generated_at,
                },
            )
            created = inserted.scalar_one_or_none() is not None
            if not created:
                existing = await connection.execute(
                    text("""
                        SELECT content_digest
                        FROM software_genome_snapshots
                        WHERE snapshot_key = :snapshot_key
                    """),
                    {"snapshot_key": snapshot_key},
                )
                existing_digest = existing.scalar_one()
                if existing_digest != digest:
                    raise SnapshotContentConflict(
                        "Snapshot identity already exists with a different content digest."
                    )

        return SnapshotWriteResult(
            snapshot_key=snapshot_key,
            content_digest=digest,
            outcome="created" if created else "already_exists",
        )
