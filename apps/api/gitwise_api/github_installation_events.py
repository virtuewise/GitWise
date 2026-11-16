"""Verified GitHub App installation lifecycle event handling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gitwise_api.github_app import (
    GitHubAppClient,
    GitHubAppInstallation,
)
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


def _repository_candidates(
    values: Any,
) -> list[dict[str, Any]]:
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


async def _verify_repositories(
    app: GitHubAppClient,
    installation: GitHubAppInstallation,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for candidate in candidates:
        full_name = candidate["full_name"]
        owner, repository_name = full_name.split("/", 1)
        repository_id = candidate["id"]
        if owner.casefold() != installation.account_login.casefold():
            raise GitHubInstallationEventError(
                "Repository owner does not match the verified installation account."
            )
        token = await app.create_repository_scoped_token(
            installation.installation_id,
            repository_id=repository_id,
            owner=owner,
            repository_name=repository_name,
        )
        if (
            len(token.repositories) != 1
            or token.repositories[0].get("id") != repository_id
            or not isinstance(token.repositories[0].get("full_name"), str)
            or token.repositories[0]["full_name"].casefold()
            != f"{owner}/{repository_name}".casefold()
            or token.permissions.get("contents") != "read"
        ):
            raise GitHubInstallationEventError(
                "GitHub did not confirm the requested repository identity and read scope."
            )
        verified.extend(token.repositories)
    return verified


async def process_installation_event(
    *,
    event_name: str,
    payload: dict[str, Any],
    app_id: int,
    app: GitHubAppClient,
    grants: InstallationGrantStore,
) -> str:
    """Apply an authenticated lifecycle event using GitHub-verified identities.

    The HTTP layer must verify the raw-body webhook signature before calling
    this function. Grant operations are idempotent so a delivery can safely be
    replayed if the process fails before its delivery record is completed.
    """
    if event_name not in ("installation", "installation_repositories"):
        raise GitHubInstallationEventError("Unsupported GitHub installation event.")
    installation_id = _installation_id(payload, app_id)
    action = payload.get("action")
    if not isinstance(action, str):
        raise GitHubInstallationEventError("Installation event action is required.")

    if event_name == "installation":
        if action in ("deleted", "suspend"):
            return f"revoked:{await grants.revoke_installation(installation_id)}"
        if action not in ("created", "unsuspend"):
            raise GitHubInstallationEventError("Unsupported installation action.")
        installation = await app.verify_installation(installation_id)
        candidates = _repository_candidates(payload.get("repositories", []))
        verified = await _verify_repositories(app, installation, candidates)
        if verified:
            count = await grants.grant_repositories(installation, verified)
            return f"granted:{count}"
        return "verified:0"

    if action not in ("added", "removed"):
        raise GitHubInstallationEventError("Unsupported installation repository action.")
    installation = await app.verify_installation(installation_id)
    added = _repository_candidates(payload.get("repositories_added", []))
    removed = _repository_candidates(payload.get("repositories_removed", []))
    added_verified = await _verify_repositories(app, installation, added)
    granted, revoked = await grants.apply_repository_changes(
        installation,
        added_verified,
        [repository["id"] for repository in removed],
    )
    return f"granted:{granted};revoked:{revoked}"
