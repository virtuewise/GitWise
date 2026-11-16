"""Verify GitHub user identity and installations visible to a GitHub App user access token."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class GitHubUserAuthenticationError(RuntimeError):
    """A GitHub user token could not establish a trusted user identity."""


@dataclass(frozen=True)
class VerifiedGitHubUser:
    user_id: int
    login: str
    installation_ids: tuple[int, ...]


class GitHubUserClient:
    """Resolve a user identity from an OAuth token held by a trusted session.

    The access token must be issued for the GitWise GitHub App and come from a
    server-managed user authorization session. Never accept it as a repository
    request parameter, log it, or persist it in the Genome.
    """

    MAX_INSTALLATIONS = 1000

    def __init__(
        self,
        access_token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("A GitHub App user access token is required.")
        self._access_token = access_token
        self._client = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {access_token}",
            },
        )
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._owns_client = client is None

    async def verify_user(self) -> VerifiedGitHubUser:
        response = await self._client.get("/user", headers=self._headers)
        if response.status_code != 200:
            raise GitHubUserAuthenticationError(
                "GitHub did not verify the authenticated user."
            )
        try:
            payload = response.json()
            user_id = payload["id"]
            login = payload["login"]
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubUserAuthenticationError(
                "GitHub returned an invalid user identity response."
            ) from exc
        if (
            type(user_id) is not int
            or user_id < 1
            or not isinstance(login, str)
            or not login
        ):
            raise GitHubUserAuthenticationError(
                "GitHub returned an invalid user identity response."
            )

        installation_ids: list[int] = []
        for page in range(1, 11):
            installations_response = await self._client.get(
                "/user/installations",
                params={"per_page": 100, "page": page},
                headers=self._headers,
            )
            if installations_response.status_code != 200:
                raise GitHubUserAuthenticationError(
                    "GitHub did not verify the user's accessible installations."
                )
            try:
                installations = installations_response.json()["installations"]
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubUserAuthenticationError(
                    "GitHub returned an invalid installation membership response."
                ) from exc
            if not isinstance(installations, list):
                raise GitHubUserAuthenticationError(
                    "GitHub returned an invalid installation membership response."
                )
            for installation in installations:
                if not isinstance(installation, dict):
                    raise GitHubUserAuthenticationError(
                        "GitHub returned an invalid installation membership entry."
                    )
                installation_id = installation.get("id")
                if type(installation_id) is not int or installation_id < 1:
                    raise GitHubUserAuthenticationError(
                        "GitHub returned an invalid installation membership entry."
                    )
                installation_ids.append(installation_id)
                if len(installation_ids) > self.MAX_INSTALLATIONS:
                    raise GitHubUserAuthenticationError(
                        "The user's installation list exceeds the supported safety limit."
                    )
            if len(installations) < 100:
                break
        else:
            if len(installation_ids) >= self.MAX_INSTALLATIONS:
                raise GitHubUserAuthenticationError(
                    "The user's installation list may be incomplete."
                )

        if len(installation_ids) != len(set(installation_ids)):
            raise GitHubUserAuthenticationError(
                "GitHub returned duplicate installation membership entries."
            )
        return VerifiedGitHubUser(
            user_id=user_id,
            login=login,
            installation_ids=tuple(installation_ids),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
