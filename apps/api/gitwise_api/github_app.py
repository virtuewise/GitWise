"""Authenticated GitHub App installation identity and token exchange."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    suspended_at: str | None = None
    permissions: dict[str, str] = field(default_factory=dict)


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

    MAX_INSTALLATION_REPOSITORIES = 5000

    def __init__(
        self,
        *,
        app_id: int,
        private_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if type(app_id) is not int or app_id < 1:
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
        if type(installation_id) is not int or installation_id < 1:
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
            returned_installation_id = payload["id"]
            returned_app_id = payload["app_id"]
            account_id = account["id"]
            account_login = account["login"]
            target_type = payload["target_type"]
            repository_selection = payload["repository_selection"]
            suspended_at = payload.get("suspended_at")
            permissions = payload.get("permissions", {})
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid installation identity response."
            ) from exc

        if (
            type(returned_installation_id) is not int
            or type(returned_app_id) is not int
            or type(account_id) is not int
            or returned_installation_id != installation_id
            or returned_app_id != self._app_id
            or account_id < 1
            or not isinstance(account_login, str)
            or not account_login
            or target_type not in ("User", "Organization")
            or repository_selection not in ("all", "selected")
            or (suspended_at is not None and not isinstance(suspended_at, str))
            or not isinstance(permissions, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in permissions.items())
        ):
            raise GitHubAppAuthenticationError(
                "The installation identity does not match the configured GitHub App."
            )

        if suspended_at is not None:
            raise GitHubAppAuthenticationError("The GitHub App installation is suspended.")

        return GitHubAppInstallation(
            installation_id=returned_installation_id,
            app_id=returned_app_id,
            account_id=account_id,
            account_login=account_login,
            target_type=target_type,
            repository_selection=repository_selection,
            suspended_at=suspended_at,
            permissions=permissions,
        )

    async def create_installation_token(
        self,
        installation_id: int,
        *,
        repositories: tuple[str, ...],
    ) -> GitHubInstallationToken:
        """Mint a short-lived token only after re-verifying App ownership."""
        if (
            not repositories
            or len(repositories) > 500
            or len(set(repositories)) != len(repositories)
            or any(
                not name or len(name) > 100 or "/" in name or name in (".", "..")
                for name in repositories
            )
        ):
            raise ValueError("A non-empty, unique repository scope is required.")
        await self.verify_installation(installation_id)
        return await self._create_installation_token(
            installation_id, repositories=repositories
        )

    async def _create_installation_token(
        self,
        installation_id: int,
        *,
        repositories: tuple[str, ...],
    ) -> GitHubInstallationToken:
        body = {"repositories": list(repositories)}
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
            or not repositories_payload
            or any(
                not isinstance(item, dict)
                or type(item.get("id")) is not int
                or item["id"] < 1
                or not isinstance(item.get("full_name"), str)
                or item["full_name"].count("/") != 1
                or item["full_name"].rsplit("/", 1)[-1] not in repositories
                for item in repositories_payload
            )
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

    async def list_installation_repositories(
        self,
        installation_id: int,
    ) -> tuple[GitHubAppInstallation, tuple[dict[str, Any], ...]]:
        """List current installation membership using a metadata-only token."""
        installation = await self.verify_installation(installation_id)
        if installation.permissions.get("contents") != "read":
            raise GitHubAppAuthenticationError(
                "The GitHub App installation does not have read-only contents permission."
            )

        response = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._app_jwt()}"},
            json={"permissions": {"metadata": "read"}},
        )
        if response.status_code != 201:
            raise GitHubAppAuthenticationError(
                "GitHub refused to create a metadata-only installation token."
            )
        try:
            token_payload = response.json()
            access_token = token_payload["token"]
            token_permissions = token_payload["permissions"]
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubAppAuthenticationError(
                "GitHub returned an invalid metadata-only token response."
            ) from exc
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(token_permissions, dict)
            or token_permissions.get("metadata") != "read"
            or any(key != "metadata" for key in token_permissions)
        ):
            raise GitHubAppAuthenticationError(
                "GitHub returned a token with an unexpected permission scope."
            )

        repositories: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        total_count: int | None = None
        for page in range(1, 51):
            page_response = await self._client.get(
                "/installation/repositories",
                params={"per_page": 100, "page": page},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if page_response.status_code != 200:
                raise GitHubAppAuthenticationError(
                    "GitHub could not list repositories accessible to the installation."
                )
            try:
                page_payload = page_response.json()
                page_total = page_payload["total_count"]
                page_repositories = page_payload["repositories"]
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubAppAuthenticationError(
                    "GitHub returned an invalid installation repository list."
                ) from exc
            if (
                type(page_total) is not int
                or page_total < 0
                or not isinstance(page_repositories, list)
                or len(page_repositories) > 100
                or (total_count is not None and page_total != total_count)
            ):
                raise GitHubAppAuthenticationError(
                    "GitHub returned an inconsistent installation repository list."
                )
            total_count = page_total
            for repository in page_repositories:
                if not isinstance(repository, dict):
                    raise GitHubAppAuthenticationError(
                        "GitHub returned invalid repository metadata."
                    )
                repository_id = repository.get("id")
                full_name = repository.get("full_name")
                is_private = repository.get("private")
                if (
                    type(repository_id) is not int
                    or repository_id < 1
                    or repository_id in seen_ids
                    or not isinstance(full_name, str)
                    or full_name.count("/") != 1
                    or not isinstance(is_private, bool)
                ):
                    raise GitHubAppAuthenticationError(
                        "GitHub returned invalid repository identity metadata."
                    )
                owner = full_name.split("/", 1)[0]
                if owner.casefold() != installation.account_login.casefold():
                    raise GitHubAppAuthenticationError(
                        "A listed repository does not belong to the installation account."
                    )
                seen_ids.add(repository_id)
                repositories.append(repository)
                if len(repositories) > self.MAX_INSTALLATION_REPOSITORIES:
                    raise GitHubAppAuthenticationError(
                        "The installation repository list exceeds the safety limit."
                    )
            if len(repositories) == total_count:
                return installation, tuple(repositories)
            if len(page_repositories) < 100:
                raise GitHubAppAuthenticationError(
                    "GitHub returned an incomplete installation repository list."
                )

        raise GitHubAppAuthenticationError(
            "The installation repository list exceeds the supported page limit."
        )

    async def create_repository_scoped_token(
        self,
        installation_id: int,
        *,
        repository_id: int,
        owner: str,
        repository_name: str,
    ) -> GitHubInstallationToken:
        """Verify immutable repository membership and mint a one-repository token."""
        if type(repository_id) is not int or repository_id < 1:
            raise ValueError("GitHub repository ID must be a positive integer.")
        if (
            not isinstance(owner, str)
            or not owner
            or "/" in owner
            or not isinstance(repository_name, str)
            or not repository_name
            or "/" in repository_name
            or len(repository_name) > 100
            or repository_name in (".", "..")
        ):
            raise ValueError("A valid GitHub owner and repository name are required.")

        installation = await self.verify_installation(installation_id)
        if installation.account_login.casefold() != owner.casefold():
            raise GitHubAppAuthenticationError(
                "The repository owner does not match the verified installation account."
            )

        token = await self._create_installation_token(
            installation_id,
            repositories=(repository_name,),
        )
        if len(token.repositories) != 1:
            raise GitHubAppAuthenticationError(
                "GitHub did not return exactly the requested repository."
            )
        repository = token.repositories[0]
        expected_full_name = f"{owner}/{repository_name}"
        if (
            repository.get("id") != repository_id
            or not isinstance(repository.get("full_name"), str)
            or repository["full_name"].casefold() != expected_full_name.casefold()
            or token.permissions.get("contents") != "read"
        ):
            raise GitHubAppAuthenticationError(
                "The installation token does not prove the requested repository identity "
                "and read scope."
            )
        return token

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
