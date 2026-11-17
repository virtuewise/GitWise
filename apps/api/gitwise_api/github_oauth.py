"""Server-side GitHub App user authorization (authorization code flow with PKCE)."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

import httpx


class GitHubOAuthError(RuntimeError):
    """The authorization code could not be exchanged. Never carries token material."""


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_in: int | None = None
    refresh_token_expires_in: int | None = None


class GitHubOAuthClient:
    """Build authorization URLs and exchange codes. Only ever used server-side."""

    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    MAX_CODE_LENGTH = 512
    MAX_REFRESH_TOKEN_LENGTH = 2048

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("GitHub App client credentials are required.")
        parsed = urlparse(redirect_uri)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("The OAuth redirect URI must use https.")
        if parsed.query or parsed.fragment:
            raise ValueError("The OAuth redirect URI must not contain a query or fragment.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self._owns_client = client is None

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> OAuthTokens:
        if not isinstance(code, str) or not 0 < len(code) <= self.MAX_CODE_LENGTH:
            raise GitHubOAuthError("GitHub authorization could not be completed.")
        response = await self._client.post(
            self.TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
        )
        failure = GitHubOAuthError("GitHub authorization could not be completed.")
        if response.status_code != 200:
            raise failure
        try:
            payload = response.json()
        except ValueError:
            raise failure from None
        if not isinstance(payload, dict) or "error" in payload:
            raise failure
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise failure
        if str(payload.get("token_type", "")).lower() != "bearer":
            raise failure
        refresh_token = payload.get("refresh_token")
        if refresh_token is not None and not (
            isinstance(refresh_token, str)
            and 0 < len(refresh_token) <= self.MAX_REFRESH_TOKEN_LENGTH
        ):
            raise failure
        expires_in = payload.get("expires_in")
        if type(expires_in) is not int or expires_in < 1:
            raise failure
        refresh_token_expires_in = payload.get("refresh_token_expires_in")
        if refresh_token is not None and (
            type(refresh_token_expires_in) is not int or refresh_token_expires_in < 1
        ):
            raise failure
        if refresh_token is None and refresh_token_expires_in is not None:
            raise failure
        return OAuthTokens(
            access_token, refresh_token, expires_in, refresh_token_expires_in
        )

    async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
        """Exchange a refresh token and return the newly rotated token pair.

        GitHub invalidates the old refresh token when this succeeds. Callers must
        serialize exchanges and persist the complete new pair atomically.
        """
        if (
            not isinstance(refresh_token, str)
            or not 0 < len(refresh_token) <= self.MAX_REFRESH_TOKEN_LENGTH
        ):
            raise GitHubOAuthError("GitHub authorization could not be refreshed.")
        try:
            response = await self._client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError:
            raise GitHubOAuthError(
                "GitHub authorization could not be refreshed."
            ) from None
        failure = GitHubOAuthError("GitHub authorization could not be refreshed.")
        if response.status_code != 200:
            raise failure
        try:
            payload = response.json()
        except ValueError:
            raise failure from None
        if not isinstance(payload, dict) or "error" in payload:
            raise failure
        access_token = payload.get("access_token")
        new_refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        refresh_expires_in = payload.get("refresh_token_expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(new_refresh_token, str)
            or not 0 < len(new_refresh_token) <= self.MAX_REFRESH_TOKEN_LENGTH
            or new_refresh_token == refresh_token
            or str(payload.get("token_type", "")).lower() != "bearer"
            or type(expires_in) is not int
            or expires_in < 1
            or type(refresh_expires_in) is not int
            or refresh_expires_in < 1
        ):
            raise failure
        return OAuthTokens(
            access_token, new_refresh_token, expires_in, refresh_expires_in
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
