import asyncio
import base64
import hashlib
import os
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from gitwise_api import main
from gitwise_api.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    STATE_COOKIE,
    AuthComponents,
)
from gitwise_api.github_oauth import GitHubOAuthClient
from gitwise_api.github_user import GitHubUserClient
from gitwise_api.sessions import SessionStore
from gitwise_api.token_crypto import TokenCipher

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")
BASE = "https://app.gitwise.test"
USERS = {
    "code-alice": ("ghu_alice_token", 1001, "alice", (11, 12)),
    "code-bob": ("ghu_bob_token", 2002, "bob", (12, 13)),
}
TOKENS = {token: (uid, login, installs) for token, uid, login, installs in USERS.values()}


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class FakeGitHub:
    """Stands in for github.com (token exchange) and api.github.com (user identity)."""

    def __init__(self) -> None:
        self.verifiers: list[str] = []
        self.exchange_error = False

    def oauth_handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.verifiers.append(form["code_verifier"])
        if self.exchange_error or form["code"] not in USERS:
            return httpx.Response(200, json={"error": "bad_verification_code"})
        token = USERS[form["code"]][0]
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "refresh_token": token.replace("ghu_", "ghr_"),
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
                "token_type": "bearer",
            },
        )

    def api_handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        user_id, login, installs = TOKENS[token]
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": user_id, "login": login})
        page = int(request.url.params["page"])
        ids = [{"id": i} for i in installs] if page == 1 else []
        return httpx.Response(200, json={"installations": ids})


def _install(github: FakeGitHub) -> SessionStore:
    store = SessionStore.from_url(DATABASE_URL, TokenCipher([Fernet.generate_key()]))
    oauth = GitHubOAuthClient(
        client_id="Iv1.test",
        client_secret="client-secret-value",
        redirect_uri=f"{BASE}/auth/github/callback",
        client=httpx.AsyncClient(transport=httpx.MockTransport(github.oauth_handler)),
    )
    main.app.state.auth_components = AuthComponents(
        sessions=store,
        oauth=oauth,
        public_base_url=BASE,
        user_client_factory=lambda token: GitHubUserClient(
            token,
            client=httpx.AsyncClient(
                base_url="https://api.github.com",
                transport=httpx.MockTransport(github.api_handler),
            ),
        ),
    )
    return store


def _browser() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://gitwise.test",
        follow_redirects=False,
    )


def _set_cookie(response: httpx.Response, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"{name} cookie was not set")


async def _sign_in(browser: httpx.AsyncClient, code: str) -> tuple[httpx.Response, str]:
    login = await browser.get("/auth/github/login")
    assert login.status_code == 302
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    callback = await browser.get(
        "/auth/github/callback", params={"code": code, "state": state}
    )
    return callback, state


pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="PostgreSQL integration database is not configured"
)


def test_login_redirects_to_github_with_pkce_and_binds_state_to_the_browser() -> None:
    async def exercise() -> None:
        store = _install(FakeGitHub())
        try:
            async with _browser() as browser:
                response = await browser.get("/auth/github/login")
            location = urlparse(response.headers["location"])
            query = parse_qs(location.query)
            assert f"{location.scheme}://{location.netloc}{location.path}" == (
                "https://github.com/login/oauth/authorize"
            )
            assert query["client_id"] == ["Iv1.test"]
            assert query["redirect_uri"] == [f"{BASE}/auth/github/callback"]
            assert query["code_challenge_method"] == ["S256"]
            assert "client_secret" not in query
            assert response.headers["cache-control"] == "no-store"

            cookie = _set_cookie(response, STATE_COOKIE)
            assert cookie.startswith(f"{STATE_COOKIE}={query['state'][0]};")
            for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/auth/github"):
                assert attribute in cookie
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_sign_in_creates_a_cookie_session_and_never_exposes_github_tokens() -> None:
    async def exercise() -> None:
        github = FakeGitHub()
        store = _install(github)
        try:
            async with _browser() as browser:
                callback, _ = await _sign_in(browser, "code-alice")
                assert callback.status_code == 303
                assert callback.headers["location"] == f"{BASE}/"

                session_cookie = _set_cookie(callback, SESSION_COOKIE)
                csrf_cookie = _set_cookie(callback, CSRF_COOKIE)
                for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
                    assert attribute in session_cookie
                assert "HttpOnly" not in csrf_cookie
                assert "SameSite=strict" in csrf_cookie
                assert "Secure" in csrf_cookie
                assert 'Max-Age=0' in _set_cookie(callback, STATE_COOKIE)

                exposed = str(callback.headers) + callback.text
                for secret in ("ghu_alice_token", "ghr_alice_token", "client-secret-value"):
                    assert secret not in exposed

                # Identity comes from the session alone; request-supplied IDs are ignored.
                me = await browser.get(
                    "/auth/session",
                    params={"user_id": 2002, "installation_id": 12},
                    headers={"x-github-user-id": "2002"},
                )
                assert me.status_code == 200
                assert me.json() == {"user_id": 1001, "login": "alice"}
                assert "ghu_" not in me.text

            assert len(github.verifiers) == 1  # code exchanged once, server-side, with PKCE
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_pkce_verifier_sent_to_github_matches_the_challenge_in_the_redirect() -> None:
    async def exercise() -> None:
        github = FakeGitHub()
        store = _install(github)
        try:
            async with _browser() as browser:
                login = await browser.get("/auth/github/login")
                query = parse_qs(urlparse(login.headers["location"]).query)
                await browser.get(
                    "/auth/github/callback",
                    params={"code": "code-alice", "state": query["state"][0]},
                )
            assert _pkce_challenge(github.verifiers[0]) == query["code_challenge"][0]
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_replayed_forged_and_unbound_oauth_state_is_rejected() -> None:
    async def exercise() -> None:
        store = _install(FakeGitHub())
        try:
            async with _browser() as browser:
                first, state = await _sign_in(browser, "code-alice")
                assert first.status_code == 303

            async with _browser() as attacker:
                # Replaying a spent state, even with a matching cookie, fails.
                attacker.cookies.set(STATE_COOKIE, state, path="/auth/github")
                replay = await attacker.get(
                    "/auth/github/callback", params={"code": "code-alice", "state": state}
                )
                assert replay.status_code == 400

                # A valid, unspent state without the browser-bound cookie fails.
                login = await attacker.get("/auth/github/login")
                fresh = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
                attacker.cookies.clear()
                unbound = await attacker.get(
                    "/auth/github/callback", params={"code": "code-alice", "state": fresh}
                )
                assert unbound.status_code == 400

                # State that was never issued fails even with a matching cookie.
                attacker.cookies.set(STATE_COOKIE, "forged", path="/auth/github")
                forged = await attacker.get(
                    "/auth/github/callback", params={"code": "code-alice", "state": "forged"}
                )
                assert forged.status_code == 400

                # An error from GitHub (for example the user declined) fails.
                attacker.cookies.set(STATE_COOKIE, fresh, path="/auth/github")
                declined = await attacker.get(
                    "/auth/github/callback",
                    params={"error": "access_denied", "state": fresh},
                )
                assert declined.status_code == 400

                for response in (replay, unbound, forged, declined):
                    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
                    assert response.json() == {
                        "detail": "GitHub sign-in could not be completed."
                    }
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_failed_code_exchange_creates_no_session() -> None:
    async def exercise() -> None:
        github = FakeGitHub()
        github.exchange_error = True
        store = _install(github)
        try:
            async with _browser() as browser:
                callback, _ = await _sign_in(browser, "code-alice")
                assert callback.status_code == 400
                assert SESSION_COOKIE not in callback.headers.get("set-cookie", "")
                assert (await browser.get("/auth/session")).status_code == 401
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_state_changing_requests_need_a_matching_csrf_token_and_logout_ends_the_session() -> None:
    async def exercise() -> None:
        store = _install(FakeGitHub())
        try:
            async with _browser() as browser:
                callback, _ = await _sign_in(browser, "code-alice")
                csrf = browser.cookies[CSRF_COOKIE]
                old_session = browser.cookies[SESSION_COOKIE]

                assert (await browser.post("/auth/logout")).status_code == 403
                wrong = await browser.post("/auth/logout", headers={"x-csrf-token": "nope"})
                assert wrong.status_code == 403
                assert (await browser.get("/auth/session")).status_code == 200

                done = await browser.post("/auth/logout", headers={"x-csrf-token": csrf})
                assert done.status_code == 204
                assert (await browser.get("/auth/session")).status_code == 401

            # The old cookie is dead server-side, not merely deleted from the browser.
            async with _browser() as replayer:
                replayer.cookies.set(SESSION_COOKIE, old_session)
                assert (await replayer.get("/auth/session")).status_code == 401
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_rotation_replaces_cookies_and_kills_the_previous_session_token() -> None:
    async def exercise() -> None:
        store = _install(FakeGitHub())
        try:
            async with _browser() as browser:
                await _sign_in(browser, "code-alice")
                old_session = browser.cookies[SESSION_COOKIE]
                old_csrf = browser.cookies[CSRF_COOKIE]

                assert (await browser.post("/auth/session/rotate")).status_code == 403
                rotated = await browser.post(
                    "/auth/session/rotate", headers={"x-csrf-token": old_csrf}
                )
                assert rotated.status_code == 204
                assert browser.cookies[SESSION_COOKIE] != old_session
                assert browser.cookies[CSRF_COOKIE] != old_csrf
                assert (await browser.get("/auth/session")).status_code == 200

                stale = await browser.post(
                    "/auth/logout", headers={"x-csrf-token": old_csrf}
                )
                assert stale.status_code == 403

            async with _browser() as replayer:
                replayer.cookies.set(SESSION_COOKIE, old_session)
                assert (await replayer.get("/auth/session")).status_code == 401
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_signing_out_everywhere_only_ends_that_users_sessions() -> None:
    async def exercise() -> None:
        store = _install(FakeGitHub())
        try:
            async with _browser() as alice, _browser() as alice_phone, _browser() as bob:
                await _sign_in(alice, "code-alice")
                await _sign_in(alice_phone, "code-alice")
                await _sign_in(bob, "code-bob")

                assert (await bob.get("/auth/session")).json()["login"] == "bob"
                response = await alice.post(
                    "/auth/logout/all", headers={"x-csrf-token": alice.cookies[CSRF_COOKIE]}
                )
                assert response.status_code == 204

                assert (await alice.get("/auth/session")).status_code == 401
                assert (await alice_phone.get("/auth/session")).status_code == 401
                still_bob = await bob.get("/auth/session")
                assert still_bob.status_code == 200
                assert still_bob.json() == {"user_id": 2002, "login": "bob"}
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_unauthenticated_requests_and_garbage_cookies_are_rejected() -> None:
    async def exercise() -> None:
        store = _install(FakeGitHub())
        try:
            async with _browser() as browser:
                assert (await browser.get("/auth/session")).status_code == 401
                browser.cookies.set(SESSION_COOKIE, "x" * 40)
                assert (await browser.get("/auth/session")).status_code == 401
                browser.cookies.set(SESSION_COOKIE, "x" * 5000)
                assert (await browser.get("/auth/session")).status_code == 401
        finally:
            await store.close()
            main.app.state.auth_components = None

    asyncio.run(exercise())


def test_sign_in_routes_report_unconfigured_instead_of_guessing() -> None:
    async def exercise() -> None:
        main.app.state.auth_components = None
        async with _browser() as browser:
            assert (await browser.get("/auth/github/login")).status_code == 503
            assert (await browser.get("/auth/session")).status_code == 503

    asyncio.run(exercise())
