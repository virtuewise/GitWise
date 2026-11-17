# ruff: noqa: E501
"""Characterization probes for the PR #12 R2 review (head 6c84644). Not for merge.

Each test PASSES while the behaviour in its name is present, so a green run is
evidence that the behaviour exists on this exact head.
"""

import asyncio
import os

import httpx
import pytest
from cryptography.fernet import Fernet
from gitwise_api.github_oauth import GitHubOAuthClient, GitHubOAuthError, OAuthTokens
from gitwise_api.sessions import SessionError, SessionStore
from gitwise_api.token_crypto import TokenCipher
from sqlalchemy import text

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database")
ACCESS, REFRESH = "ghu_probe_access", "ghr_probe_refresh"


class RecordingGitHub:
    def __init__(self, *, fail_revocation: bool = False) -> None:
        self.fail = fail_revocation
        self.token_revocations: list[str] = []
        self.grant_revocations: list[str] = []
        self.refreshes = 0

    async def revoke_access_token(self, *, access_token: str) -> None:
        self.token_revocations.append(access_token)
        if self.fail:
            raise GitHubOAuthError("revoke failed")

    async def revoke_authorization(self, *, access_token: str) -> None:
        self.grant_revocations.append(access_token)
        if self.fail:
            raise GitHubOAuthError("revoke failed")

    async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
        self.refreshes += 1
        raise AssertionError("unexpected refresh")


def _store() -> SessionStore:
    return SessionStore.from_url(DATABASE_URL, TokenCipher([Fernet.generate_key()]))


def _user() -> int:
    return int.from_bytes(os.urandom(5), "big") + 1


async def _session(store: SessionStore, user_id: int, access: str = ACCESS):
    issued = await store.create_session(
        github_user_id=user_id,
        github_login="probe",
        access_token=access,
        refresh_token=REFRESH,
        access_token_expires_in=28800,
        refresh_token_expires_in=15897600,
    )
    session = await store.resolve_session(issued.session_token)
    assert session is not None
    return issued, session


async def _row(store: SessionStore, session_id):
    async with store._engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT revoked_at, encrypted_refresh_token, access_token_expires_at, "
                    "refresh_token_expires_at FROM user_sessions WHERE session_id = :s"
                ),
                {"s": session_id},
            )
        ).one()


def test_probe_logout_all_sends_the_sentinel_and_makes_one_github_call_per_stored_row() -> None:
    async def exercise() -> None:
        store = _store()
        user = _user()
        try:
            for _ in range(4):  # four earlier sessions, each logged out normally
                old, _ = await _session(store, user)
                await store.revoke_session(old.session_token, RecordingGitHub())
            live, live_session = await _session(store, user, access="ghu_the_live_token")

            github = RecordingGitHub(fail_revocation=True)  # e.g. the live token expired
            newly = await store.revoke_user_sessions(user, github)

            assert newly == 1
            # Every row is tried until one succeeds. Already-revoked rows decrypt to the
            # sentinel string, which is sent to GitHub as if it were an access token.
            assert len(github.grant_revocations) == 5
            assert github.grant_revocations.count("revoked-session") == 4
            assert "ghu_the_live_token" in github.grant_revocations
            # Local state is sanitized regardless.
            row = await _row(store, live_session.session_id)
            assert row[0] is not None and row[1] is None
        finally:
            await store.close()

    asyncio.run(exercise())


def test_probe_logout_revokes_the_refreshed_token_when_it_races_an_in_flight_refresh() -> None:
    async def exercise() -> None:
        started = asyncio.Event()

        class SlowRefresh(RecordingGitHub):
            async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
                started.set()
                await asyncio.sleep(1.5)
                return OAuthTokens("ghu_new_token", "ghr_new_token", 28800, 15897600)

        store = _store()
        try:
            issued, session = await _session(store, _user())
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET access_token_expires_at = now() + interval '30 seconds' "
                        "WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            github = SlowRefresh()
            refresh = asyncio.create_task(store.access_token(session, github))
            await started.wait()
            logout = asyncio.create_task(store.revoke_session(issued.session_token, github))
            returned = await refresh
            assert await logout is True

            # The in-flight request got the new token, and logout waited for the commit,
            # saw the *new* ciphertext, and revoked that one at GitHub, not the stale one.
            assert returned == "ghu_new_token"
            assert github.token_revocations == ["ghu_new_token"]
            row = await _row(store, session.session_id)
            assert row[0] is not None and row[1] is None and row[2] is None and row[3] is None
            with pytest.raises(SessionError):
                await store.access_token(session, github)
        finally:
            await store.close()

    asyncio.run(exercise())


def test_probe_a_github_5xx_during_refresh_revokes_the_session() -> None:
    async def exercise() -> None:
        github = GitHubOAuthClient(
            client_id="Iv1.test",
            client_secret="secret",
            redirect_uri="https://app.example.com/auth/github/callback",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(503))
            ),
        )
        store = _store()
        try:
            _, session = await _session(store, _user())
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET access_token_expires_at = now() + interval '30 seconds' "
                        "WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            with pytest.raises(SessionError):
                await store.access_token(session, github)
            # One transient 503 ends the session and erases its refresh token.
            row = await _row(store, session.session_id)
            assert row[0] is not None and row[1] is None
        finally:
            await github.close()
            await store.close()

    asyncio.run(exercise())


def test_probe_logout_sends_a_stored_access_token_that_may_already_be_expired_without_refreshing() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            issued, session = await _session(store, _user())
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET access_token_expires_at = now() - interval '2 hours' "
                        "WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            github = RecordingGitHub()
            assert await store.revoke_session(issued.session_token, github) is True
            # The (expired) token is sent as-is; there is no refresh-first step, so GitHub
            # may reject it and the 6-month refresh token is then orphaned, not revoked.
            assert github.token_revocations == [ACCESS]
            assert github.refreshes == 0
        finally:
            await store.close()

    asyncio.run(exercise())
