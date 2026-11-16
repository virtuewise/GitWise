import asyncio
import json
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from gitwise_api.github_app import (
    GitHubAppAuthenticationError,
    GitHubAppClient,
)


def make_app_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_key = private_key.public_key()
    return private_pem, public_key


def test_verifies_installation_ownership_and_scopes_token() -> None:
    async def exercise() -> None:
        private_pem, public_key = make_app_key()
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            bearer = request.headers["Authorization"].removeprefix("Bearer ")
            claims = jwt.decode(
                bearer,
                public_key,
                algorithms=["RS256"],
                issuer="123",
                options={"verify_exp": False},
            )
            assert claims["iat"] <= int(datetime.now(UTC).timestamp())
            assert claims["exp"] - claims["iat"] <= 600
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": 456,
                        "app_id": 123,
                        "account": {"id": 789, "login": "acme"},
                        "target_type": "Organization",
                        "repository_selection": "selected",
                    },
                )
            assert request.method == "POST"
            assert json.loads(request.content) == {"repositories": ["service-api"]}
            return httpx.Response(
                201,
                json={
                    "token": "short-lived-installation-token",
                    "expires_at": "2026-11-17T00:00:00Z",
                    "permissions": {"contents": "read"},
                    "repositories": [{"id": 999, "full_name": "acme/service-api"}],
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=transport,
        ) as http_client:
            app = GitHubAppClient(
                app_id=123,
                private_key=private_pem,
                client=http_client,
            )
            installation = await app.verify_installation(456)
            token = await app.create_installation_token(
                456,
                repositories=("service-api",),
            )

        assert installation.installation_id == 456
        assert installation.app_id == 123
        assert installation.account_id == 789
        assert installation.account_login == "acme"
        assert installation.repository_selection == "selected"
        assert token.token == "short-lived-installation-token"
        assert token.permissions == {"contents": "read"}
        assert token.repositories == ({"id": 999, "full_name": "acme/service-api"},)
        assert [request.method for request in seen] == ["GET", "GET", "POST"]

    asyncio.run(exercise())


def test_rejects_installation_owned_by_another_app() -> None:
    async def exercise() -> None:
        private_pem, _ = make_app_key()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": 456,
                    "app_id": 999,
                    "account": {"id": 789, "login": "acme"},
                    "target_type": "Organization",
                    "repository_selection": "all",
                },
            )

        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            app = GitHubAppClient(
                app_id=123,
                private_key=private_pem,
                client=http_client,
            )
            with pytest.raises(GitHubAppAuthenticationError, match="does not match"):
                await app.verify_installation(456)

    asyncio.run(exercise())


def test_rejects_invalid_installation_token_repository_scope() -> None:
    async def exercise() -> None:
        private_pem, _ = make_app_key()
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "id": 456,
                        "app_id": 123,
                        "account": {"id": 789, "login": "acme"},
                        "target_type": "Organization",
                        "repository_selection": "all",
                    },
                ),
            ),
        ) as http_client:
            app = GitHubAppClient(
                app_id=123,
                private_key=private_pem,
                client=http_client,
            )
            with pytest.raises(ValueError, match="repository scope"):
                await app.create_installation_token(
                    456,
                    repositories=("../unsafe",),
                )
            with pytest.raises(ValueError, match="repository scope"):
                await app.create_installation_token(456, repositories=())

    asyncio.run(exercise())


def test_requires_valid_app_configuration() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        GitHubAppClient(app_id=0, private_key="key")
    with pytest.raises(ValueError, match="private key"):
        GitHubAppClient(app_id=123, private_key="  ")


def test_repository_scoped_token_verifies_immutable_repository_id() -> None:
    async def exercise() -> None:
        private_pem, _ = make_app_key()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": 456,
                        "app_id": 123,
                        "account": {"id": 789, "login": "acme"},
                        "target_type": "Organization",
                        "repository_selection": "selected",
                    },
                )
            return httpx.Response(
                201,
                json={
                    "token": "repository-scoped-token",
                    "expires_at": "2026-11-17T00:00:00Z",
                    "permissions": {"contents": "read", "metadata": "read"},
                    "repositories": [
                        {"id": 999, "full_name": "acme/service-api"}
                    ],
                },
            )

        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            app = GitHubAppClient(
                app_id=123,
                private_key=private_pem,
                client=http_client,
            )
            token = await app.create_repository_scoped_token(
                456,
                repository_id=999,
                owner="acme",
                repository_name="service-api",
            )
            assert token.repositories[0]["id"] == 999

            with pytest.raises(
                GitHubAppAuthenticationError, match="repository identity"
            ):
                await app.create_repository_scoped_token(
                    456,
                    repository_id=1000,
                    owner="acme",
                    repository_name="service-api",
                )

            with pytest.raises(
                GitHubAppAuthenticationError, match="installation account"
            ):
                await app.create_repository_scoped_token(
                    456,
                    repository_id=999,
                    owner="other-org",
                    repository_name="service-api",
                )

    asyncio.run(exercise())


def test_rejects_non_integer_installation_identity_fields() -> None:
    async def exercise() -> None:
        private_pem, _ = make_app_key()
        invalid_payloads = [
            {
                "id": True,
                "app_id": 123,
                "account": {"id": 789, "login": "acme"},
                "target_type": "Organization",
                "repository_selection": "all",
            },
            {
                "id": 456,
                "app_id": "123",
                "account": {"id": 789, "login": "acme"},
                "target_type": "Organization",
                "repository_selection": "all",
            },
            {
                "id": 456,
                "app_id": 123,
                "account": {"id": True, "login": "acme"},
                "target_type": "Organization",
                "repository_selection": "all",
            },
        ]
        for payload in invalid_payloads:
            async with httpx.AsyncClient(
                base_url="https://api.github.com",
                transport=httpx.MockTransport(
                    lambda request, body=payload: httpx.Response(200, json=body)
                ),
            ) as http_client:
                app = GitHubAppClient(
                    app_id=123,
                    private_key=private_pem,
                    client=http_client,
                )
                with pytest.raises(
                    GitHubAppAuthenticationError, match="does not match"
                ):
                    await app.verify_installation(456)

    asyncio.run(exercise())

def test_repository_identity_rejects_boolean_numeric_ids() -> None:
    async def exercise() -> None:
        private_pem, _ = make_app_key()
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        ) as http_client:
            app = GitHubAppClient(
                app_id=123,
                private_key=private_pem,
                client=http_client,
            )
            with pytest.raises(ValueError, match="positive integer"):
                await app.create_repository_scoped_token(
                    456,
                    repository_id=True,
                    owner="acme",
                    repository_name="service-api",
                )
            with pytest.raises(ValueError, match="positive integer"):
                await app.verify_installation(True)

    asyncio.run(exercise())

def test_rejects_suspended_installation() -> None:
    async def exercise() -> None:
        private_pem, _ = make_app_key()
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "id": 456,
                        "app_id": 123,
                        "account": {"id": 789, "login": "acme"},
                        "target_type": "Organization",
                        "repository_selection": "selected",
                        "suspended_at": "2026-11-16T12:00:00Z",
                    },
                )
            ),
        ) as http_client:
            app = GitHubAppClient(
                app_id=123,
                private_key=private_pem,
                client=http_client,
            )
            with pytest.raises(GitHubAppAuthenticationError, match="suspended"):
                await app.verify_installation(456)

    asyncio.run(exercise())
