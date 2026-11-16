import asyncio
import os
from uuid import uuid4

import pytest
from gitwise_api.github_app import GitHubAppInstallation
from gitwise_api.installation_grants import InstallationGrantStore

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration database is not configured")
def test_installation_grants_are_idempotent_and_isolated() -> None:
    async def exercise() -> None:
        from sqlalchemy import text

        first_installation_id = uuid4().int % 1_000_000_000 + 1
        second_installation_id = uuid4().int % 1_000_000_000 + 1_000_000_001
        first_repository_id = uuid4().int % 1_000_000_000_000 + 1
        second_repository_id = uuid4().int % 1_000_000_000_000 + 1_000_000_001
        first = GitHubAppInstallation(
            installation_id=first_installation_id,
            app_id=123,
            account_id=789,
            account_login="acme",
            target_type="Organization",
            repository_selection="selected",
        )
        second = GitHubAppInstallation(
            installation_id=second_installation_id,
            app_id=456,
            account_id=789,
            account_login="acme",
            target_type="Organization",
            repository_selection="selected",
        )
        store = InstallationGrantStore.from_url(DATABASE_URL)
        try:
            first_repositories = [
                {
                    "id": first_repository_id,
                    "full_name": "acme/service-api",
                    "private": True,
                },
                {
                    "id": second_repository_id,
                    "full_name": "acme/web-app",
                    "private": False,
                },
            ]
            assert await store.apply_repository_changes(first, first_repositories, []) == (2, 0)
            assert await store.apply_repository_changes(first, first_repositories, []) == (2, 0)
            assert (
                await store.grant_repositories(
                    second,
                    [first_repositories[0]],
                )
                == 1
            )

            assert await store.reconcile_installation_repositories(
                first, [first_repositories[1]]
            ) == (1, 1)

            async with store._engine.connect() as connection:
                active = await connection.execute(
                    text("""
                        SELECT principal_id, provider_repository_id
                        FROM repository_access_grants
                        WHERE provider = 'github'
                          AND principal_type = 'github_app_installation'
                          AND provider_repository_id IN (:repo_one, :repo_two)
                          AND revoked_at IS NULL
                    """),
                    {
                        "repo_one": first_repository_id,
                        "repo_two": second_repository_id,
                    },
                )
                active_rows = {
                    (row.principal_id, row.provider_repository_id)
                    for row in active
                }
            assert active_rows == {
                (str(first_installation_id), second_repository_id),
                (str(second_installation_id), first_repository_id),
            }

            assert await store.revoke_installation(first_installation_id) == 1
            assert await store.revoke_installation(second_installation_id) == 1
        finally:
            async with store._engine.begin() as connection:
                await connection.execute(
                    text("""
                        DELETE FROM provider_repositories
                        WHERE provider = 'github'
                          AND provider_repository_id IN (:repo_one, :repo_two)
                    """),
                    {
                        "repo_one": first_repository_id,
                        "repo_two": second_repository_id,
                    },
                )
            await store.close()

    asyncio.run(exercise())
