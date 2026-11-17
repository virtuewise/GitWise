# ruff: noqa: E501
"""Characterization probes for the PR #12 adversarial review.

Each test PASSES while the behaviour described in its name is present, so a green
run is evidence the behaviour exists. They are not intended to be merged.
"""

import asyncio
import os

import pytest
from cryptography.fernet import Fernet
from gitwise_api.github_oauth import GitHubOAuthError, OAuthTokens
from gitwise_api.sessions import SessionError, SessionStore
from gitwise_api.token_crypto import TokenCipher
from sqlalchemy import text

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database")
ACCESS, REFRESH = "ghu_probe_access", "ghr_probe_refresh"


async def _near_expiry_session(store: SessionStore):
    issued = await store.create_session(
        github_user_id=int.from_bytes(os.urandom(5), "big") + 1,
        github_login="probe",
        access_token=ACCESS,
        refresh_token=REFRESH,
        access_token_expires_in=28800,
        refresh_token_expires_in=15897600,
    )
    session = await store.resolve_session(issued.session_token)
    assert session is not None
    async with store._engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE user_sessions SET access_token_expires_at = now() + interval '30 seconds' "
                "WHERE session_id = :s"
            ),
            {"s": session.session_id},
        )
    return issued, session


def test_probe_timeout_after_github_rotation_strands_the_session_and_every_request_retries_github() -> None:
    async def exercise() -> None:
        class GitHubThatRotatesThenTimesOut:
            """First call: GitHub consumes the refresh token, but the response is lost."""

            valid_refresh = REFRESH
            calls = 0

            async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
                self.calls += 1
                if refresh_token != self.valid_refresh:
                    raise GitHubOAuthError("bad_refresh_token")  # GitHub: already used
                self.valid_refresh = "ghr_issued_but_never_received"
                raise GitHubOAuthError("read timeout")  # what the client does on httpx errors

        store = SessionStore.from_url(DATABASE_URL, TokenCipher([Fernet.generate_key()]))
        fake = GitHubThatRotatesThenTimesOut()
        try:
            _, session = await _near_expiry_session(store)
            for _ in range(4):
                with pytest.raises(SessionError):
                    await store.access_token(session, fake)
            # Fail-closed as designed, but nothing marks the session as dead: all four
            # requests went to GitHub, three of them with a refresh token GitHub had
            # already consumed.
            assert fake.calls == 4
            async with store._engine.connect() as connection:
                revoked = (
                    await connection.execute(
                        text("SELECT revoked_at FROM user_sessions WHERE session_id = :s"),
                        {"s": session.session_id},
                    )
                ).scalar_one()
            assert revoked is None
        finally:
            await store.close()

    asyncio.run(exercise())


def test_probe_logout_keeps_decryptable_access_and_refresh_tokens_in_the_row() -> None:
    async def exercise() -> None:
        store = SessionStore.from_url(DATABASE_URL, TokenCipher([Fernet.generate_key()]))
        try:
            issued, session = await _near_expiry_session(store)
            assert await store.revoke_session(issued.session_token) is True
            async with store._engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT revoked_at, encrypted_access_token, encrypted_refresh_token "
                            "FROM user_sessions WHERE session_id = :s"
                        ),
                        {"s": session.session_id},
                    )
                ).one()
            assert row[0] is not None
            assert store._cipher.decrypt(row[1]) == ACCESS
            assert store._cipher.decrypt(row[2]) == REFRESH
        finally:
            await store.close()

    asyncio.run(exercise())


def test_probe_a_waiting_refresh_blocks_without_bound_while_the_row_lock_is_held() -> None:
    async def exercise() -> None:
        class NeverCalled:
            async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
                raise AssertionError("must not be reached while the lock is held")

        store = SessionStore.from_url(DATABASE_URL, TokenCipher([Fernet.generate_key()]))
        try:
            _, session = await _near_expiry_session(store)
            holder = await store._engine.connect()
            transaction = await holder.begin()
            await holder.execute(
                text("SELECT 1 FROM user_sessions WHERE session_id = :s FOR UPDATE"),
                {"s": session.session_id},
            )
            try:
                # A second worker waits on the lock. There is no lock_timeout, so it only
                # returns when the holder releases it; here we give up after 3 seconds.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(store.access_token(session, NeverCalled()), 3)
            finally:
                await transaction.rollback()
                await holder.close()
        finally:
            await store.close()

    asyncio.run(exercise())


def test_probe_resolve_session_and_logout_queue_behind_an_in_flight_refresh() -> None:
    async def exercise() -> None:
        started = asyncio.Event()

        class SlowGitHub:
            async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
                started.set()
                await asyncio.sleep(2)
                return OAuthTokens("ghu_new", "ghr_new", 28800, 15897600)

        store = SessionStore.from_url(DATABASE_URL, TokenCipher([Fernet.generate_key()]))
        try:
            issued, session = await _near_expiry_session(store)
            refresh = asyncio.create_task(store.access_token(session, SlowGitHub()))
            await started.wait()
            loop = asyncio.get_running_loop()
            begun = loop.time()
            assert await store.resolve_session(issued.session_token) is not None
            waited = loop.time() - begun
            await refresh
            # An ordinary authenticated request for this session was held for the
            # remainder of the provider call.
            assert waited > 1.0
        finally:
            await store.close()

    asyncio.run(exercise())


def test_probe_legacy_row_with_refresh_token_but_no_refresh_expiry_cannot_refresh() -> None:
    async def exercise() -> None:
        class NeverCalled:
            async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
                raise AssertionError("legacy rows must not be refreshed")

        store = SessionStore.from_url(DATABASE_URL, TokenCipher([Fernet.generate_key()]))
        try:
            _, session = await _near_expiry_session(store)
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET refresh_token_expires_at = NULL "
                        "WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            with pytest.raises(SessionError):
                await store.access_token(session, NeverCalled())
            # A token inside the safety window is still returned while it is valid.
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET access_token_expires_at = now() + interval '1 hour' "
                        "WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            assert await store.access_token(session, NeverCalled()) == ACCESS
        finally:
            await store.close()

    asyncio.run(exercise())
