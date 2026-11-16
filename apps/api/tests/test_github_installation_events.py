import asyncio
from contextlib import asynccontextmanager

import pytest
from gitwise_api.github_app import GitHubAppInstallation, GitHubInstallationToken
from gitwise_api.github_installation_events import (
    GitHubInstallationEventError,
    process_installation_event,
)


INSTALLATION = GitHubAppInstallation(
    installation_id=456,
    app_id=123,
    account_id=789,
    account_login="acme",
    target_type="Organization",
    repository_selection="selected",
)


class FakeGitHubApp:
    def __init__(self) -> None:
        self.verified: list[int] = []
        self.scoped: list[tuple[int, str, str]] = []

    async def verify_installation(
        self, installation_id: int, *, allow_suspended: bool = False
    ) -> GitHubAppInstallation:
        self.verified.append(installation_id)
        return INSTALLATION

    async def list_installation_repositories(self, installation_id):
        self.verified.append(installation_id)
        return INSTALLATION, (
            {"id": 9001, "full_name": "acme/api", "private": True},
            {"id": 9003, "full_name": "acme/web", "private": False},
        )

    async def create_repository_scoped_token(
        self,
        installation_id: int,
        *,
        repository_id: int,
        owner: str,
        repository_name: str,
    ) -> GitHubInstallationToken:
        self.scoped.append((repository_id, owner, repository_name))
        return GitHubInstallationToken(
            token="ephemeral-test-token",
            expires_at="2026-11-17T00:00:00Z",
            permissions={"contents": "read", "metadata": "read"},
            repositories=(
                {
                    "id": repository_id,
                    "full_name": f"{owner}/{repository_name}",
                    "private": True,
                },
            ),
        )


class FakeGrantStore:
    def __init__(self) -> None:
        self.granted: list[tuple[int, ...]] = []
        self.revoked: list[tuple[int, tuple[int, ...]]] = []
        self.installation_revocations: list[int] = []
        self.atomic_changes: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
        self.reconciliations: list[tuple[int, ...]] = []

    async def grant_repositories(self, installation, repositories):
        self.granted.append(tuple(item["id"] for item in repositories))
        return len(repositories)

    @asynccontextmanager
    async def installation_lock(self, installation_id):
        yield

    async def reconcile_installation_repositories(self, installation, repositories):
        values = tuple(item["id"] for item in repositories)
        self.reconciliations.append(values)
        return len(values), 1

    async def revoke_repositories(self, installation_id, repository_ids):
        values = tuple(repository_ids)
        self.revoked.append((installation_id, values))
        return len(values)

    async def revoke_installation(self, installation_id):
        self.installation_revocations.append(installation_id)
        return 2


def test_added_and_removed_repositories_use_verified_ids_and_atomic_grants() -> None:
    async def exercise() -> None:
        app = FakeGitHubApp()
        grants = FakeGrantStore()
        result = await process_installation_event(
            event_name="installation_repositories",
            payload={
                "action": "added",
                "repository_selection": "selected",
                "installation": {"id": 456, "app_id": 123},
                "repositories_added": [
                    {
                        "id": 9001,
                        "full_name": "acme/api",
                        "name": "api",
                    }
                ],
                "repositories_removed": [{"id": 9002, "full_name": "acme/old"}],
            },
            app_id=123,
            app=app,
            grants=grants,
        )
        assert result == "reconciled:2;revoked:1"
        assert app.verified == [456]
        assert app.scoped == []
        assert grants.reconciliations == [(9001, 9003)]

    asyncio.run(exercise())


def test_deleted_installation_revokes_all_without_requiring_live_installation() -> None:
    async def exercise() -> None:
        app = FakeGitHubApp()
        grants = FakeGrantStore()
        result = await process_installation_event(
            event_name="installation",
            payload={
                "action": "deleted",
                "installation": {"id": 456, "app_id": 123},
            },
            app_id=123,
            app=app,
            grants=grants,
        )
        assert result == "revoked:2"
        assert grants.installation_revocations == [456]
        assert app.verified == []

    asyncio.run(exercise())


def test_created_installation_provisions_only_verified_repositories() -> None:
    async def exercise() -> None:
        app = FakeGitHubApp()
        grants = FakeGrantStore()
        result = await process_installation_event(
            event_name="installation",
            payload={
                "action": "created",
                "installation": {"id": 456, "app_id": 123},
                "repositories": [
                    {"id": 9001, "full_name": "acme/api"},
                ],
            },
            app_id=123,
            app=app,
            grants=grants,
        )
        assert result == "reconciled:2;revoked:1"
        assert grants.reconciliations == [(9001, 9003)]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "installation",
    [
        {"id": True, "app_id": 123},
        {"id": 456, "app_id": "123"},
        {"id": 456, "app_id": 999},
    ],
)

def test_unsuspend_reconciles_current_installation_repositories() -> None:
    async def exercise() -> None:
        app = FakeGitHubApp()
        grants = FakeGrantStore()
        result = await process_installation_event(
            event_name="installation",
            payload={
                "action": "unsuspend",
                "installation": {"id": 456, "app_id": 123},
            },
            app_id=123,
            app=app,
            grants=grants,
        )
        assert result == "reconciled:2;revoked:1"
        assert grants.reconciliations == [(9001, 9003)]

    asyncio.run(exercise())

def test_rejects_untrusted_installation_identity(installation) -> None:
    async def exercise() -> None:
        with pytest.raises(GitHubInstallationEventError):
            await process_installation_event(
                event_name="installation",
                payload={"action": "deleted", "installation": installation},
                app_id=123,
                app=FakeGitHubApp(),
                grants=FakeGrantStore(),
            )

    asyncio.run(exercise())


def test_rejects_unknown_installation_repository_action() -> None:
    async def exercise() -> None:
        with pytest.raises(
            GitHubInstallationEventError, match="Unsupported installation repository action"
        ):
            await process_installation_event(
                event_name="installation_repositories",
                payload={
                    "action": "edited",
                    "installation": {"id": 456, "app_id": 123},
                },
                app_id=123,
                app=FakeGitHubApp(),
                grants=FakeGrantStore(),
            )

    asyncio.run(exercise())


def test_rejects_malformed_repository_event_lists() -> None:
    async def exercise() -> None:
        for repositories in (
            [
                {"id": 9001, "full_name": "acme/api"},
                {"id": 9001, "full_name": "acme/other"},
            ],
            [{"id": True, "full_name": "acme/api"}],
            [{"id": 9001, "full_name": "acme/api/extra"}],
        ):
            with pytest.raises(GitHubInstallationEventError):
                await process_installation_event(
                    event_name="installation_repositories",
                    payload={
                        "action": "added",
                        "repository_selection": "selected",
                        "installation": {"id": 456, "app_id": 123},
                        "repositories_added": repositories,
                        "repositories_removed": [],
                    },
                    app_id=123,
                    app=FakeGitHubApp(),
                    grants=FakeGrantStore(),
                )

    asyncio.run(exercise())


def test_repository_payload_is_not_authoritative_for_current_grant_set() -> None:
    async def exercise() -> None:
        app = FakeGitHubApp()
        grants = FakeGrantStore()
        result = await process_installation_event(
            event_name="installation_repositories",
            payload={
                "action": "added",
                "repository_selection": "selected",
                "installation": {"id": 456, "app_id": 123},
                "repositories_added": [{"id": 9999, "full_name": "acme/forged"}],
                "repositories_removed": [],
            },
            app_id=123,
            app=app,
            grants=grants,
        )
        assert result == "reconciled:2;revoked:1"
        assert grants.reconciliations == [(9001, 9003)]

    asyncio.run(exercise())

def test_all_to_selected_scope_change_preserves_currently_selected_repositories() -> None:
    async def exercise() -> None:
        app = FakeGitHubApp()
        grants = FakeGrantStore()
        result = await process_installation_event(
            event_name="installation_repositories",
            payload={
                "action": "removed",
                "repository_selection": "selected",
                "installation": {"id": 456, "app_id": 123},
                "repositories_added": [],
                "repositories_removed": [],
            },
            app_id=123,
            app=app,
            grants=grants,
        )
        assert result == "reconciled:2;revoked:1"
        assert app.verified == [456]
        assert grants.reconciliations == [(9001, 9003)]
        assert grants.installation_revocations == []
        assert grants.atomic_changes == []

    asyncio.run(exercise())

