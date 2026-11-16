import asyncio

import httpx
import pytest
from gitwise_api.github_user import (
    GitHubUserAuthenticationError,
    GitHubUserClient,
)


def test_verifies_github_user_and_accessible_installations() -> None:
    async def exercise() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.headers["Authorization"] == "Bearer session-managed-token"
            if request.url.path == "/user":
                return httpx.Response(200, json={"id": 789, "login": "maintainer"})
            page = int(request.url.params["page"])
            if page == 1:
                return httpx.Response(
                    200,
                    json={
                        "installations": [
                            {"id": 456},
                            {"id": 457},
                        ]
                    },
                )
            return httpx.Response(200, json={"installations": []})

        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = GitHubUserClient(
                "session-managed-token",
                client=http_client,
            )
            user = await client.verify_user()

        assert user.user_id == 789
        assert user.login == "maintainer"
        assert user.installation_ids == (456, 457)
        assert [request.url.path for request in requests] == [
            "/user",
            "/user/installations",
        ]

    asyncio.run(exercise())


def test_pages_through_full_installation_pages() -> None:
    async def exercise() -> None:
        pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/user":
                return httpx.Response(200, json={"id": 789, "login": "maintainer"})
            page = int(request.url.params["page"])
            pages.append(page)
            if page == 1:
                return httpx.Response(
                    200, json={"installations": [{"id": n} for n in range(1, 101)]}
                )
            return httpx.Response(200, json={"installations": [{"id": 101}]})

        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = GitHubUserClient("session-managed-token", client=http_client)
            user = await client.verify_user()

        assert pages == [1, 2]
        assert user.installation_ids == tuple(range(1, 102))

    asyncio.run(exercise())


def test_rejects_invalid_user_identity_and_installation_membership() -> None:
    async def exercise() -> None:
        responses = [
            httpx.Response(200, json={"id": True, "login": "maintainer"}),
            httpx.Response(200, json={"id": 789, "login": "maintainer"}),
            httpx.Response(200, json={"installations": [{"id": True}]}),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = GitHubUserClient("session-managed-token", client=http_client)
            with pytest.raises(GitHubUserAuthenticationError, match="user identity"):
                await client.verify_user()

        async def invalid_installation() -> None:
            def installation_handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/user":
                    return httpx.Response(200, json={"id": 789, "login": "maintainer"})
                return httpx.Response(200, json={"installations": [{"id": True}]})

            async with httpx.AsyncClient(
                base_url="https://api.github.com",
                transport=httpx.MockTransport(installation_handler),
            ) as http_client:
                client = GitHubUserClient("session-managed-token", client=http_client)
                with pytest.raises(
                    GitHubUserAuthenticationError, match="membership entry"
                ):
                    await client.verify_user()

        await invalid_installation()

    asyncio.run(exercise())


def test_requires_session_managed_access_token() -> None:
    with pytest.raises(ValueError, match="GitHub App user access token"):
        GitHubUserClient(" ")
