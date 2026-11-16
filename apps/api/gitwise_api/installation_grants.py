"""Transactional GitHub App installation grants.

Callers must pass installation identity returned by GitHubAppClient.verify_installation
and repository records returned by create_repository_scoped_token. This module
never treats webhook payload fields or user-supplied principal IDs as identity.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gitwise_api.github_app import GitHubAppInstallation


class InstallationGrantError(ValueError):
    """Verified installation or repository metadata is invalid."""


class InstallationGrantStore:
    """Provision and revoke read-only installation grants transactionally."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def grant_repositories(
        self,
        installation: GitHubAppInstallation,
        repositories: Sequence[dict[str, object]],
    ) -> int:
        """Grant read access only to repositories verified through GitHub.

        The repository IDs and names must come from the GitHub installation
        token response, not the webhook's untrusted JSON body.
        """
        if installation.installation_id < 1 or installation.account_id < 1:
            raise InstallationGrantError("Verified installation identity is required.")
        if not repositories:
            raise InstallationGrantError("At least one verified repository is required.")

        normalized: dict[int, tuple[str, str, bool]] = {}
        for repository in repositories:
            repository_id = repository.get("id")
            full_name = repository.get("full_name")
            is_private = repository.get("private", False)
            if (
                type(repository_id) is not int
                or repository_id < 1
                or not isinstance(full_name, str)
                or full_name.count("/") != 1
                or not isinstance(is_private, bool)
            ):
                raise InstallationGrantError("GitHub returned invalid repository metadata.")
            owner, name = full_name.split("/", 1)
            if (
                not owner
                or not name
                or owner.casefold() != installation.account_login.casefold()
            ):
                raise InstallationGrantError(
                    "Repository owner does not match the verified installation account."
                )
            normalized[repository_id] = (owner, name, is_private)

        if len(normalized) != len(repositories):
            raise InstallationGrantError("Duplicate repository IDs are not allowed.")

        async with self._engine.begin() as connection:
            for repository_id, (owner, name, is_private) in normalized.items():
                canonical_url = f"https://github.com/{owner}/{name}"
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
                        "repository_id": repository_id,
                        "owner": owner,
                        "name": name,
                        "url": canonical_url,
                        "is_private": is_private,
                    },
                )
                await connection.execute(
                    text("""
                        INSERT INTO repository_access_grants (
                            grant_id, provider, provider_repository_id,
                            principal_type, principal_id, permission, granted_by
                        ) VALUES (
                            gen_random_uuid(), 'github', :repository_id,
                            'github_app_installation', :installation_id, 'read',
                            :granted_by
                        )
                        ON CONFLICT (
                            provider, provider_repository_id, principal_type, principal_id
                        ) WHERE revoked_at IS NULL
                        DO UPDATE SET
                            permission = EXCLUDED.permission,
                            granted_by = EXCLUDED.granted_by
                    """),
                    {
                        "repository_id": repository_id,
                        "installation_id": str(installation.installation_id),
                        "granted_by": f"github_app:{installation.app_id}",
                    },
                )
        return len(normalized)

    async def revoke_repositories(
        self,
        installation_id: int,
        repository_ids: Sequence[int],
    ) -> int:
        """Revoke only the listed repositories; preserve unrelated grants."""
        self._validate_installation_id(installation_id)
        ids = self._validated_repository_ids(repository_ids)
        if not ids:
            return 0
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("""
                    UPDATE repository_access_grants
                    SET revoked_at = now()
                    WHERE provider = 'github'
                      AND principal_type = 'github_app_installation'
                      AND principal_id = :installation_id
                      AND provider_repository_id = ANY(:repository_ids)
                      AND revoked_at IS NULL
                """),
                {
                    "installation_id": str(installation_id),
                    "repository_ids": ids,
                },
            )
            return result.rowcount

    async def revoke_installation(self, installation_id: int) -> int:
        """Revoke every active grant owned by a deleted/suspended installation."""
        self._validate_installation_id(installation_id)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("""
                    UPDATE repository_access_grants
                    SET revoked_at = now()
                    WHERE provider = 'github'
                      AND principal_type = 'github_app_installation'
                      AND principal_id = :installation_id
                      AND revoked_at IS NULL
                """),
                {"installation_id": str(installation_id)},
            )
            return result.rowcount

    @staticmethod
    def _validate_installation_id(installation_id: int) -> None:
        if type(installation_id) is not int or installation_id < 1:
            raise InstallationGrantError("A positive verified installation ID is required.")

    @staticmethod
    def _validated_repository_ids(repository_ids: Sequence[int]) -> list[int]:
        ids = list(repository_ids)
        if any(type(repository_id) is not int or repository_id < 1 for repository_id in ids):
            raise InstallationGrantError("Repository IDs must be positive integers.")
        if len(ids) != len(set(ids)):
            raise InstallationGrantError("Duplicate repository IDs are not allowed.")
        return ids
