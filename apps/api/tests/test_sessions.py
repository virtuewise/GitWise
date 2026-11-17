import asyncio
import base64
import hashlib
import os
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from gitwise_api.sessions import AuthorizationStateError, SessionStore
from gitwise_api.token_crypto import TokenCipher
from sqlalchemy import text

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="PostgreSQL integration database is not configured"
)

ACCESS = "ghu_access_token_value"
REFRESH = "ghr_refresh_token_value"


def _store(**options) -> SessionStore:
    return SessionStore.from_url(
        DATABASE_URL, TokenCipher([Fernet.generate_key()]), **options
    )


def _user_id() -> int:
    return int.from_bytes(os.urandom(5), "big") + 1


def test_oauth_state_is_single_use_and_returns_the_pkce_verifier() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            start = await store.begin_authorization()
            verifier = await store.consume_authorization(start.state)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert challenge == start.code_challenge

            with pytest.raises(AuthorizationStateError):
                await store.consume_authorization(start.state)
            with pytest.raises(AuthorizationStateError):
                await store.consume_authorization("never-issued")
            with pytest.raises(AuthorizationStateError):
                await store.consume_authorization("")
        finally:
            await store.close()

    asyncio.run(exercise())


def test_concurrent_oauth_state_replay_succeeds_exactly_once() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            start = await store.begin_authorization()
            results = await asyncio.gather(
                *(store.consume_authorization(start.state) for _ in range(5)),
                return_exceptions=True,
            )
            winners = [r for r in results if isinstance(r, str)]
            failures = [r for r in results if isinstance(r, AuthorizationStateError)]
            assert len(winners) == 1
            assert len(failures) == 4
        finally:
            await store.close()

    asyncio.run(exercise())


def test_expired_oauth_state_is_rejected() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            start = await store.begin_authorization()
            digest = hashlib.sha256(start.state.encode()).digest()
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE oauth_authorization_states "
                        "SET created_at = now() - interval '2 hours', "
                        "    expires_at = now() - interval '1 hour' "
                        "WHERE state_hash = :h"
                    ),
                    {"h": digest},
                )
            with pytest.raises(AuthorizationStateError):
                await store.consume_authorization(start.state)
        finally:
            await store.close()

    asyncio.run(exercise())


def test_session_lifecycle_and_tokens_are_encrypted_at_rest() -> None:
    async def exercise() -> None:
        store = _store()
        user_id = _user_id()
        try:
            issued = await store.create_session(
                github_user_id=user_id,
                github_login="maintainer",
                access_token=ACCESS,
                access_token_expires_in=28800,
                refresh_token=REFRESH,
                refresh_token_expires_in=15897600,
            )
            session = await store.resolve_session(issued.session_token)
            assert session is not None
            assert (session.github_user_id, session.github_login) == (user_id, "maintainer")
            assert await store.access_token(session, object()) == ACCESS

            assert SessionStore.csrf_matches(session, issued.csrf_token)
            assert not SessionStore.csrf_matches(session, "wrong")
            assert not SessionStore.csrf_matches(session, None)
            assert not SessionStore.csrf_matches(session, issued.session_token)

            async with store._engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT token_hash, encrypted_access_token, "
                            "encrypted_refresh_token FROM user_sessions "
                            "WHERE session_id = :s"
                        ),
                        {"s": session.session_id},
                    )
                ).one()
            stored = b"".join(bytes(part) for part in row)
            for secret in (ACCESS, REFRESH, issued.session_token, issued.csrf_token):
                assert secret.encode() not in stored

            assert await store.resolve_session("not-a-session") is None
            assert await store.resolve_session("") is None
        finally:
            await store.close()

    asyncio.run(exercise())


def test_idle_and_absolute_expiry_fail_closed() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            idle = await store.create_session(
                github_user_id=_user_id(), github_login="idle", access_token=ACCESS
            )
            absolute = await store.create_session(
                github_user_id=_user_id(), github_login="absolute", access_token=ACCESS
            )
            idle_session = await store.resolve_session(idle.session_token)
            absolute_session = await store.resolve_session(absolute.session_token)
            assert idle_session and absolute_session

            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET idle_expires_at = now() - interval '1 second' "
                        "WHERE session_id = :s"
                    ),
                    {"s": idle_session.session_id},
                )
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET created_at = now() - interval '2 days', "
                        "absolute_expires_at = now() - interval '1 second', "
                        "idle_expires_at = now() - interval '1 second' "
                        "WHERE session_id = :s"
                    ),
                    {"s": absolute_session.session_id},
                )

            assert await store.resolve_session(idle.session_token) is None
            assert await store.resolve_session(absolute.session_token) is None
            assert await store.rotate_session(idle.session_token) is None
            from gitwise_api.sessions import SessionError

            with pytest.raises(SessionError):
                await store.access_token(idle_session, object())
        finally:
            await store.close()

    asyncio.run(exercise())


def test_activity_extends_idle_window_but_never_past_the_absolute_limit() -> None:
    async def exercise() -> None:
        from datetime import timedelta

        store = _store(idle_ttl=timedelta(hours=1), absolute_ttl=timedelta(hours=2))
        try:
            issued = await store.create_session(
                github_user_id=_user_id(), github_login="active", access_token=ACCESS
            )
            session = await store.resolve_session(issued.session_token)
            assert session
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions "
                        "SET idle_expires_at = now() + interval '4 minutes', "
                        "    absolute_expires_at = now() + interval '5 minutes' "
                        "WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            assert await store.resolve_session(issued.session_token)
            async with store._engine.connect() as connection:
                capped = (
                    await connection.execute(
                        text(
                            "SELECT idle_expires_at = absolute_expires_at FROM user_sessions "
                            "WHERE session_id = :s"
                        ),
                        {"s": session.session_id},
                    )
                ).scalar_one()
            assert capped is True
        finally:
            await store.close()

    asyncio.run(exercise())


def test_rotation_invalidates_previous_tokens_immediately() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="rotator",
                access_token=ACCESS,
                access_token_expires_in=28800,
            )
            rotated = await store.rotate_session(issued.session_token)
            assert rotated is not None
            assert rotated.session_token != issued.session_token

            assert await store.resolve_session(issued.session_token) is None
            assert await store.rotate_session(issued.session_token) is None
            new_session = await store.resolve_session(rotated.session_token)
            assert new_session is not None
            assert SessionStore.csrf_matches(new_session, rotated.csrf_token)
            assert not SessionStore.csrf_matches(new_session, issued.csrf_token)
            assert await store.access_token(new_session, object()) == ACCESS
        finally:
            await store.close()

    asyncio.run(exercise())


def test_logout_and_user_wide_invalidation_do_not_cross_users() -> None:
    async def exercise() -> None:
        class NoopRevoker:
            async def revoke_access_token(self, *, access_token: str) -> None:
                return None

            async def revoke_authorization(self, *, access_token: str) -> None:
                return None

        oauth = NoopRevoker()
        store = _store()
        alice_id, bob_id = _user_id(), _user_id()
        try:
            alice_one = await store.create_session(
                github_user_id=alice_id, github_login="alice", access_token=ACCESS
            )
            alice_two = await store.create_session(
                github_user_id=alice_id, github_login="alice", access_token=ACCESS
            )
            bob = await store.create_session(
                github_user_id=bob_id, github_login="bob", access_token=ACCESS
            )

            assert await store.revoke_session(alice_one.session_token, oauth) is True
            assert await store.revoke_session(alice_one.session_token, oauth) is False
            assert await store.resolve_session(alice_one.session_token) is None
            assert await store.resolve_session(alice_two.session_token) is not None

            assert await store.revoke_user_sessions(alice_id, oauth) == 1
            assert await store.resolve_session(alice_two.session_token) is None
            bob_session = await store.resolve_session(bob.session_token)
            assert bob_session is not None
            assert bob_session.github_user_id == bob_id
        finally:
            await store.close()

    asyncio.run(exercise())


def test_rejects_unverified_identity_inputs() -> None:
    async def exercise() -> None:
        from gitwise_api.sessions import SessionError

        store = _store()
        try:
            for bad_id in (0, -1, True, "7"):
                with pytest.raises(SessionError):
                    await store.create_session(
                        github_user_id=bad_id, github_login="x", access_token=ACCESS
                    )
            with pytest.raises(SessionError):
                await store.create_session(
                    github_user_id=1, github_login="", access_token=ACCESS
                )
        finally:
            await store.close()

    asyncio.run(exercise())



def test_expired_access_token_is_refreshed_and_rotated_atomically() -> None:
    async def exercise() -> None:
        from gitwise_api.github_oauth import OAuthTokens

        class FakeOAuth:
            calls = 0

            async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
                self.calls += 1
                assert refresh_token == REFRESH
                await asyncio.sleep(0.05)
                return OAuthTokens(
                    "ghu_rotated_access",
                    "ghr_rotated_refresh",
                    28800,
                    15897600,
                )

        store = _store()
        fake = FakeOAuth()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="refreshable",
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
                        "UPDATE user_sessions SET access_token_expires_at = "
                        "now() + interval '30 seconds' WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )

            results = await asyncio.gather(
                *(store.access_token(session, fake) for _ in range(4))
            )
            assert results == ["ghu_rotated_access"] * 4
            assert fake.calls == 1

            async with store._engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT encrypted_access_token, encrypted_refresh_token, "
                            "access_token_expires_at, refresh_token_expires_at "
                            "FROM user_sessions WHERE session_id = :s"
                        ),
                        {"s": session.session_id},
                    )
                ).one()
            assert store._cipher.decrypt(row[0]) == "ghu_rotated_access"
            assert store._cipher.decrypt(row[1]) == "ghr_rotated_refresh"
            async with store._engine.connect() as connection:
                live = (
                    await connection.execute(
                        text(
                            "SELECT access_token_expires_at > now() + interval '7 hours' "
                            "AND refresh_token_expires_at > now() + interval '180 days' "
                            "FROM user_sessions WHERE session_id = :s"
                        ),
                        {"s": session.session_id},
                    )
                ).scalar_one()
            assert live is True

        finally:
            await store.close()

    asyncio.run(exercise())


def test_expired_refresh_token_fails_closed_without_provider_call() -> None:
    async def exercise() -> None:
        from gitwise_api.sessions import SessionError

        class NeverCalled:
            async def refresh_access_token(self, *, refresh_token: str):
                raise AssertionError("expired refresh token must not be sent to GitHub")

        store = _store()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="expired-refresh",
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
                        "UPDATE user_sessions SET access_token_expires_at = now() - "
                        "interval '1 second', refresh_token_expires_at = now() - "
                        "interval '1 second' WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            with pytest.raises(SessionError):
                await store.access_token(session, NeverCalled())
        finally:
            await store.close()

    asyncio.run(exercise())


def test_revoked_session_cannot_refresh() -> None:
    async def exercise() -> None:
        from gitwise_api.sessions import SessionError

        class NeverCalled:
            async def refresh_access_token(self, *, refresh_token: str):
                raise AssertionError("revoked session must not refresh")

        store = _store()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="revoked",
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
                        "UPDATE user_sessions SET revoked_at = now(), "
                        "access_token_expires_at = now() - interval '1 second' "
                        "WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            with pytest.raises(SessionError):
                await store.access_token(session, NeverCalled())
        finally:
            await store.close()

    asyncio.run(exercise())


def test_unknown_access_token_expiry_fails_closed() -> None:
    async def exercise() -> None:
        from gitwise_api.sessions import SessionError

        store = _store()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="unknown-expiry",
                access_token=ACCESS,
            )
            session = await store.resolve_session(issued.session_token)
            assert session is not None
            with pytest.raises(SessionError):
                await store.access_token(session, object())
        finally:
            await store.close()

    asyncio.run(exercise())


def test_provider_refresh_failure_never_returns_the_expired_access_token() -> None:
    async def exercise() -> None:
        from gitwise_api.github_oauth import GitHubOAuthError
        from gitwise_api.sessions import SessionError

        class FailedRefresh:
            async def refresh_access_token(self, *, refresh_token: str):
                raise GitHubOAuthError("GitHub authorization could not be refreshed.")

        store = _store()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="provider-failure",
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
                        "UPDATE user_sessions SET access_token_expires_at = "
                        "now() - interval '1 second' WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )
            with pytest.raises(SessionError):
                await store.access_token(session, FailedRefresh())

            async with store._engine.connect() as connection:
                revoked = (
                    await connection.execute(
                        text(
                            "SELECT revoked_at IS NOT NULL "
                            "AND encrypted_refresh_token IS NULL "
                            "AND access_token_expires_at IS NULL "
                            "AND refresh_token_expires_at IS NULL, "
                            "encrypted_access_token "
                            "FROM user_sessions WHERE session_id = :s"
                        ),
                        {"s": session.session_id},
                    )
                ).one()
            assert revoked[0] is True
            assert store._cipher.decrypt(revoked[1]) == "revoked-session"
        finally:
            await store.close()

    asyncio.run(exercise())



def test_session_expiring_during_refresh_fails_closed() -> None:
    async def exercise() -> None:
        from gitwise_api.github_oauth import OAuthTokens
        from gitwise_api.sessions import SessionError

        class SlowOAuth:
            async def refresh_access_token(self, *, refresh_token: str) -> OAuthTokens:
                assert refresh_token == REFRESH
                await asyncio.sleep(1.2)
                return OAuthTokens(
                    "ghu_rotated_access",
                    "ghr_rotated_refresh",
                    28800,
                    15897600,
                )

        store = _store(
            idle_ttl=timedelta(seconds=1),
            absolute_ttl=timedelta(seconds=5),
        )
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="expires-during-refresh",
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
                        "UPDATE user_sessions SET access_token_expires_at = "
                        "now() + interval '30 seconds' WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )

            with pytest.raises(SessionError):
                await store.access_token(session, SlowOAuth())
        finally:
            await store.close()

    asyncio.run(exercise())


def test_connect_failure_keeps_refresh_credentials_retryable() -> None:
    async def exercise() -> None:
        from gitwise_api.github_oauth import GitHubOAuthConnectError
        from gitwise_api.sessions import SessionError

        class ConnectFailure:
            async def refresh_access_token(self, *, refresh_token: str):
                assert refresh_token == REFRESH
                raise GitHubOAuthConnectError(
                    "GitHub authorization could not be refreshed."
                )

        store = _store()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(),
                github_login="connect-failure",
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
                        "UPDATE user_sessions SET access_token_expires_at = "
                        "now() - interval '1 second' WHERE session_id = :s"
                    ),
                    {"s": session.session_id},
                )

            with pytest.raises(SessionError):
                await store.access_token(session, ConnectFailure())

            async with store._engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT revoked_at, encrypted_refresh_token "
                            "FROM user_sessions WHERE session_id = :s"
                        ),
                        {"s": session.session_id},
                    )
                ).one()
            assert row[0] is None
            assert store._cipher.decrypt(row[1]) == REFRESH
        finally:
            await store.close()

    asyncio.run(exercise())


def test_terminal_session_purge_is_bounded_and_respects_retention_window() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            revoked_old = await store.create_session(
                github_user_id=_user_id(), github_login="revoked-old", access_token=ACCESS
            )
            expired_old = await store.create_session(
                github_user_id=_user_id(), github_login="expired-old", access_token=ACCESS
            )
            revoked_recent = await store.create_session(
                github_user_id=_user_id(), github_login="revoked-recent", access_token=ACCESS
            )
            active = await store.create_session(
                github_user_id=_user_id(), github_login="still-active", access_token=ACCESS
            )
            async with store._engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET revoked_at = now() - interval '31 days' "
                        "WHERE token_hash = :token_hash"
                    ),
                    {"token_hash": hashlib.sha256(revoked_old.session_token.encode()).digest()},
                )
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET created_at = now() - interval '32 days', "
                        "idle_expires_at = now() - interval '31 days', "
                        "absolute_expires_at = now() - interval '31 days' "
                        "WHERE token_hash = :token_hash"
                    ),
                    {"token_hash": hashlib.sha256(expired_old.session_token.encode()).digest()},
                )
                await connection.execute(
                    text(
                        "UPDATE user_sessions SET revoked_at = now() - interval '29 days' "
                        "WHERE token_hash = :token_hash"
                    ),
                    {"token_hash": hashlib.sha256(revoked_recent.session_token.encode()).digest()},
                )

            with pytest.raises(ValueError):
                await store.purge_terminal_sessions(batch_size=0)
            with pytest.raises(ValueError):
                await store.purge_terminal_sessions(batch_size=5001)

            assert await store.purge_terminal_sessions(batch_size=1) == 1
            assert await store.purge_terminal_sessions(batch_size=1) == 1
            assert await store.purge_terminal_sessions(batch_size=1) == 0

            async with store._engine.connect() as connection:
                remaining = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM user_sessions "
                            "WHERE token_hash IN (:recent, :active)"
                        ),
                        {
                            "recent": hashlib.sha256(
                                revoked_recent.session_token.encode()
                            ).digest(),
                            "active": hashlib.sha256(active.session_token.encode()).digest(),
                        },
                    )
                ).scalar_one()
                removed = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM user_sessions "
                            "WHERE token_hash IN (:revoked, :expired)"
                        ),
                        {
                            "revoked": hashlib.sha256(revoked_old.session_token.encode()).digest(),
                            "expired": hashlib.sha256(expired_old.session_token.encode()).digest(),
                        },
                    )
                ).scalar_one()
            assert remaining == 2
            assert removed == 0
        finally:
            await store.close()

    asyncio.run(exercise())


def test_terminal_session_purge_skips_rows_locked_by_live_requests() -> None:
    async def exercise() -> None:
        store = _store()
        try:
            issued = await store.create_session(
                github_user_id=_user_id(), github_login="locked-terminal", access_token=ACCESS
            )
            async with store._engine.begin() as locker:
                await locker.execute(
                    text(
                        "UPDATE user_sessions SET revoked_at = now() - interval '31 days' "
                        "WHERE token_hash = :token_hash"
                    ),
                    {"token_hash": hashlib.sha256(issued.session_token.encode()).digest()},
                )
                await locker.execute(
                    text(
                        "SELECT session_id FROM user_sessions "
                        "WHERE token_hash = :token_hash FOR UPDATE"
                    ),
                    {"token_hash": hashlib.sha256(issued.session_token.encode()).digest()},
                )
                # A separate pooled connection must not wait on this row.
                assert await store.purge_terminal_sessions(batch_size=10) == 0
            assert await store.purge_terminal_sessions(batch_size=10) == 1
        finally:
            await store.close()

    asyncio.run(exercise())


def test_logout_all_never_sends_sanitized_sentinels_to_github() -> None:
    async def exercise() -> None:
        class Revoker:
            def __init__(self) -> None:
                self.grant_tokens: list[str] = []

            async def revoke_access_token(self, *, access_token: str) -> None:
                return None

            async def revoke_authorization(self, *, access_token: str) -> None:
                self.grant_tokens.append(access_token)
                return None

        store = _store()
        user_id = _user_id()
        try:
            revoker = Revoker()
            for _ in range(3):
                issued = await store.create_session(
                    github_user_id=user_id,
                    github_login="sentinel-test",
                    access_token=ACCESS,
                    refresh_token=REFRESH,
                    access_token_expires_in=28800,
                    refresh_token_expires_in=15897600,
                )
                assert await store.revoke_session(issued.session_token, revoker) is True

            await store.create_session(
                github_user_id=user_id,
                github_login="sentinel-test",
                access_token="ghu_current_access",
                refresh_token="ghr_current_refresh",
                access_token_expires_in=28800,
                refresh_token_expires_in=15897600,
            )
            assert await store.revoke_user_sessions(user_id, revoker) == 1
            assert revoker.grant_tokens == ["ghu_current_access"]
            assert "revoked-session" not in revoker.grant_tokens
        finally:
            await store.close()

    asyncio.run(exercise())
