"""Verified GitHub App installation lifecycle event handling."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

from gitwise_api.github_app import GitHubAppClient
from gitwise_api.installation_grants import InstallationGrantStore

MAX_REPOSITORIES_PER_EVENT = 500


class GitHubInstallationEventError(ValueError):
    """A signed installation event is malformed or not valid for this App."""


def _installation_id(payload: Mapping[str, Any], app_id: int) -> int:
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        raise GitHubInstallationEventError("Installation event is missing installation identity.")
    installation_id = installation.get("id")
    event_app_id = installation.get("app_id")
    if (
        type(installation_id) is not int
        or installation_id < 1
        or type(event_app_id) is not int
        or event_app_id != app_id
    ):
        raise GitHubInstallationEventError(
            "Signed installation identity does not match the configured GitHub App."
        )
    return installation_id


def _repository_candidates(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > MAX_REPOSITORIES_PER_EVENT:
        raise GitHubInstallationEventError("Installation repository list is invalid or too large.")
    candidates: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for value in values:
        if not isinstance(value, dict):
            raise GitHubInstallationEventError("Installation repository entry is invalid.")
        repository_id = value.get("id")
        full_name = value.get("full_name")
        if (
            type(repository_id) is not int
            or repository_id < 1
            or repository_id in seen_ids
            or not isinstance(full_name, str)
            or full_name.count("/") != 1
        ):
            raise GitHubInstallationEventError("Installation repository identity is invalid.")
        seen_ids.add(repository_id)
        candidates.append(value)
    return candidates


async def process_installation_event(
    *,
    event_name: str,
    payload: dict[str, Any],
    app_id: int,
    app: GitHubAppClient,
    grants: InstallationGrantStore,
) -> str:
    """Apply signed lifecycle events against GitHub's current installation state.

    The HTTP layer must verify the raw-body webhook signature before calling
    this function. A PostgreSQL advisory lock serializes deliveries per
    installation. Current GitHub membership, not event ordering, determines
    the resulting grant set.
    """
    if event_name not in ("installation", "installation_repositories"):
        raise GitHubInstallationEventError("Unsupported GitHub installation event.")
    installation_id = _installation_id(payload, app_id)
    async with grants.installation_lock(installation_id):
        return await _process_installation_event_locked(
            event_name=event_name,
            payload=payload,
            app_id=app_id,
            installation_id=installation_id,
            app=app,
            grants=grants,
        )


async def _process_installation_event_locked(
    *,
    event_name: str,
    payload: dict[str, Any],
    app_id: int,
    installation_id: int,
    app: GitHubAppClient,
    grants: InstallationGrantStore,
) -> str:
    action = payload.get("action")
    if not isinstance(action, str):
        raise GitHubInstallationEventError("Installation event action is required.")

    if event_name == "installation":
        if action == "deleted":
            return f"revoked:{await grants.revoke_installation(installation_id)}"
        if action == "suspend":
            installation = await app.verify_installation(
                installation_id, allow_suspended=True
            )
            if installation.suspended_at is not None:
                return f"revoked:{await grants.revoke_installation(installation_id)}"
            # A delayed suspend event must not revoke a currently active installation.
            installation, repositories = await app.list_installation_repositories(
                installation_id
            )
            granted, revoked = await grants.reconcile_installation_repositories(
                installation, repositories
            )
            return f"reconciled:{granted};revoked:{revoked}"
        if action not in ("created", "unsuspend"):
            raise GitHubInstallationEventError("Unsupported installation action.")

        installation, repositories = await app.list_installation_repositories(
            installation_id
        )
        granted, revoked = await grants.reconcile_installation_repositories(
            installation, repositories
        )
        return f"reconciled:{granted};revoked:{revoked}"

    if action not in ("added", "removed"):
        raise GitHubInstallationEventError("Unsupported installation repository action.")
    repository_selection = payload.get("repository_selection")
    if repository_selection not in ("all", "selected"):
        raise GitHubInstallationEventError("Repository selection must be all or selected.")
    if "repositories_added" not in payload or "repositories_removed" not in payload:
        raise GitHubInstallationEventError("Installation repository lists are required.")
    _repository_candidates(payload["repositories_added"])
    _repository_candidates(payload["repositories_removed"])

    installation, repositories = await app.list_installation_repositories(
        installation_id
    )
    granted, revoked = await grants.reconcile_installation_repositories(
        installation, repositories
    )
    return f"reconciled:{granted};revoked:{revoked}"
