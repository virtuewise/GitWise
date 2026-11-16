"""Authenticated GitHub App installation identity and token exchange."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt


class GitHubAppAuthenticationError(RuntimeError):
    """GitHub App credentials or installation identity could not be verified."""


@dataclass(frozen=True)
class GitHubAppInstallation:
    installation_id: int
    app_id: int
    account_id: int
    account_login: str
    target_type: str
    repository_selection: str


@dataclass(frozen=True)
class GitHubInstallationToken:
    token: str
    expires_at: str
    permissions: dict[str, str]
    repositories: tuple[dict[str, Any], ...]


class GitHubAppClient:
    """Authenticate as one GitHub App and verify its installation ownership.

    The installation endpoint is queried with an App JWT before any
    installation-scoped token is requested. Tokens should be kept in memory,
    scoped to the smallest repository set, and never logged or persisted.
    """

    def __init__(
        self,
        *,
        app_id: int,
        private_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if app_id < 1:
            raise ValueError("GitHub App ID must be a positive integer.")
        if not private_key.strip():
            raise ValueError("GitHub App private key must be configured.")
        self._app_id = app_id
        self._private_key = private_key.replace("\\n", "\n")
        self._client = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._owns_client = client is None

    def _app_jwt(self) -> str:
        now = datetime.now(UTC)
        claims = {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=8)).timestamp()),
            "iss": str(self._app_id),
        }
        token = jwt.encode(claims, self._private_key, algorithm="RS256")
        return token.decode("ascii") if isinstance(token, bytes) else token

    async def verify_installation(self, installation_id: int) -> GitHubAppInstallation:
        """Prove that the installation exists and belongs to this exact App."""
        if installation_id < 1:
            raise ValueError("GitHub installation ID must be a positive integer.")
        response = await self._client.get(
            f"/app/installations/{installation_id}",
            headers={"Authorization": f"Bearer {self._app_jwt()}"},
        )
        if response.status_code != 200:
            raise GitHubAppAuthenticationError(
                "GitHub did not verify this installation for the configured App."
            )
        try:
            payload = response.json()
            account = payload["account"]
            returned_installation_id = int(payload["id"])
            returned_app_id = int(payload["app_id"])
            account_id = int(account["id"])
            account_login = account["login"]
            target_type = payload["target_type"]
            repository_selection = payload["repository_selection"]
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid installation identity response."
            ) from exc

        if (
            returned_installation_id != installation_id
            or returned_app_id != self._app_id
            or account_id < 1
            or not isinstance(account_login, str)
            or not account_login
            or target_type not in ("User", "Organization")
            or repository_selection not in ("all", "selected")
        ):
            raise GitHubAppAuthenticationError(
                "The installation identity does not match the configured GitHub App."
            )

        return GitHubAppInstallation(
            installation_id=returned_installation_id,
            app_id=returned_app_id,
            account_id=account_id,
            account_login=account_login,
            target_type=target_type,
            repository_selection=repository_selection,
        )

    async def create_installation_token(
        self,
        installation_id: int,
        *,
        repositories: tuple[str, ...] = (),
    ) -> GitHubInstallationToken:
        """Mint a short-lived token only after re-verifying App ownership."""
        await self.verify_installation(installation_id)
        if len(repositories) > 500 or any(
            not name or "/" in name or name in (".", "..") for name in repositories
        ):
            raise ValueError("Repository scope must contain valid repository names.")
        body = {"repositories": list(repositories)} if repositories else {}
        response = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._app_jwt()}"},
            json=body,
        )
        if response.status_code != 201:
            raise GitHubAppAuthenticationError(
                "GitHub refused to create an installation access token."
            )
        try:
            payload = response.json()
            token = payload["token"]
            expires_at = payload["expires_at"]
            permissions = payload["permissions"]
            repositories_payload = payload.get("repositories", [])
        except (KeyError, TypeError) as exc:
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid installation token response."
            ) from exc
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(expires_at, str)
            or not isinstance(permissions, dict)
            or not isinstance(repositories_payload, list)
        ):
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid installation token response."
            )
        return GitHubInstallationToken(
            token=token,
            expires_at=expires_at,
            permissions=permissions,
            repositories=tuple(
                item for item in repositories_payload if isinstance(item, dict)
            ),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
