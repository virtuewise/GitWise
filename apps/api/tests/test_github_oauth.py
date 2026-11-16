import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from gitwise_api.github_oauth import GitHubOAuthClient, GitHubOAuthError

REDIRECT = "https://app.example.com/auth/github/callback"


def _client(handler) -> GitHubOAuthClient:
    return GitHubOAuthClient(
        client_id="Iv1.abc",
        client_secret="shh",
        redirect_uri=REDIRECT,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://app.example.com/cb",
        "https://app.example.com/cb?next=/x",
        "https://app.example.com/cb#frag",
        "javascript:alert(1)",
    ],
)
def test_rejects_unsafe_redirect_uris(redirect_uri: str) -> None:
    with pytest.raises(ValueError):
        GitHubOAuthClient(client_id="a", client_secret="b", redirect_uri=redirect_uri)


def test_allows_plain_http_only_for_localhost() -> None:
    GitHubOAuthClient(
        client_id="a", client_secret="b", redirect_uri="http://localhost:8000/auth/github/callback"
    )


def test_authorization_url_carries_pkce_and_no_secret() -> None:
    client = _client(lambda request: httpx.Response(500))
    url = urlparse(client.authorization_url(state="s", code_challenge="c"))
    query = parse_qs(url.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [REDIRECT]
    assert "shh" not in url.query


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"error": "bad_verification_code"}),
        httpx.Response(200, json={"access_token": "t", "token_type": "mac"}),
        httpx.Response(200, json={"token_type": "bearer"}),
        httpx.Response(200, json={"access_token": "t", "token_type": "bearer", "expires_in": True}),
        httpx.Response(200, text="not json"),
    ],
)
def test_exchange_failures_are_uniform_and_leak_nothing(response: httpx.Response) -> None:
    async def exercise() -> None:
        client = _client(lambda request: response)
        with pytest.raises(GitHubOAuthError) as failure:
            await client.exchange_code(code="abc", code_verifier="v")
        assert "shh" not in str(failure.value)

    asyncio.run(exercise())


def test_exchange_sends_secret_and_verifier_in_the_body_and_parses_tokens() -> None:
    async def exercise() -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            assert str(request.url) == GitHubOAuthClient.TOKEN_URL
            return httpx.Response(
                200,
                json={
                    "access_token": "ghu_x",
                    "refresh_token": "ghr_y",
                    "expires_in": 28800,
                    "token_type": "Bearer",
                },
            )

        tokens = await _client(handler).exchange_code(code="abc", code_verifier="ver")
        assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == (
            "ghu_x",
            "ghr_y",
            28800,
        )
        assert seen["code_verifier"] == "ver"
        assert seen["client_secret"] == "shh"
        assert "ghu_x" not in repr(tokens)

    asyncio.run(exercise())


def test_oversized_authorization_code_is_rejected_before_any_request() -> None:
    async def exercise() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be made")

        with pytest.raises(GitHubOAuthError):
            await _client(handler).exchange_code(code="x" * 600, code_verifier="v")

    asyncio.run(exercise())
