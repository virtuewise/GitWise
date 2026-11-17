"""One-time OAuth state and server-managed user sessions.

Browsers only ever hold an opaque session token and a CSRF token. GitHub user
tokens stay on the server, encrypted at rest, and are never returned to callers
except through ``SessionStore.access_token`` for server-side GitHub calls.
Expiry decisions use the database clock so workers cannot disagree about time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gitwise_api.github_oauth import (
    GitHubOAuthClient,
    GitHubOAuthConnectError,
    GitHubOAuthError,
)
from gitwise_api.token_crypto import TokenCipher, TokenCryptoError

MAX_TOKEN_LENGTH = 256


class AuthorizationStateError(RuntimeError):
    """OAuth state is unknown, expired, or already used. Deliberately uniform."""


class SessionError(RuntimeError):
    """A session could not be created or changed."""


@dataclass(frozen=True)
class AuthorizationStart:
    state: str
    code_challenge: str


@dataclass(frozen=True)
class IssuedSession:
    session_token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: UUID
    github_user_id: int
    github_login: str
    csrf_hash: bytes


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _plausible_token(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_LENGTH


async def _database_time(connection) -> datetime:
    return (await connection.execute(text("SELECT now()"))).scalar_one()


class SessionStore:
    """PostgreSQL-backed OAuth state and session lifecycle."""

    def __init__(
        self,
        engine: AsyncEngine,
        cipher: TokenCipher,
        *,
        state_ttl: timedelta = timedelta(minutes=10),
        idle_ttl: timedelta = timedelta(hours=8),
        absolute_ttl: timedelta = timedelta(hours=24),
    ) -> None:
        if idle_ttl > absolute_ttl:
            raise ValueError("Idle session lifetime cannot exceed the absolute lifetime.")
        self._engine = engine
        self._cipher = cipher
        self._state_ttl = state_ttl
        self._idle_ttl = idle_ttl
        self._absolute_ttl = absolute_ttl

    @classmethod
    def from_url(cls, database_url: str, cipher: TokenCipher, **options) -> SessionStore:
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("GITWISE_DATABASE_URL must use postgresql+asyncpg.")
        return cls(create_async_engine(database_url, pool_pre_ping=True), cipher, **options)

    async def close(self) -> None:
        await self._engine.dispose()

    # OAuth state -----------------------------------------------------------

    async def begin_authorization(self) -> AuthorizationStart:
        """Create single-use state and a PKCE challenge; the verifier stays server-side."""
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM oauth_authorization_states "
                    "WHERE expires_at < now() - interval '1 day'"
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO oauth_authorization_states
                        (state_hash, encrypted_code_verifier, expires_at)
                    VALUES (:state_hash, :verifier, now() + make_interval(secs => :ttl))
                    """
                ),
                {
                    "state_hash": _digest(state),
                    "verifier": self._cipher.encrypt(verifier),
                    "ttl": self._state_ttl.total_seconds(),
                },
            )
        return AuthorizationStart(state=state, code_challenge=challenge)

    async def consume_authorization(self, state: str) -> str:
        """Atomically spend a state value and return its PKCE verifier.

        Unknown, expired and replayed state all fail identically.
        """
        if not _plausible_token(state):
            raise AuthorizationStateError("Invalid OAuth state.")
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE oauth_authorization_states
                        SET consumed_at = now()
                        WHERE state_hash = :state_hash
                          AND consumed_at IS NULL
                          AND expires_at > now()
                        RETURNING encrypted_code_verifier
                        """
                    ),
                    {"state_hash": _digest(state)},
                )
            ).first()
        if row is None:
            raise AuthorizationStateError("Invalid OAuth state.")
        try:
            return self._cipher.decrypt(row[0])
        except TokenCryptoError:
            raise AuthorizationStateError("Invalid OAuth state.") from None

    # Sessions --------------------------------------------------------------

    async def create_session(
        self,
        *,
        github_user_id: int,
        github_login: str,
        access_token: str,
        refresh_token: str | None = None,
        access_token_expires_in: int | None = None,
        refresh_token_expires_in: int | None = None,
    ) -> IssuedSession:
        """Create a session for an identity GitHub has already verified."""
        if type(github_user_id) is not int or github_user_id < 1:
            raise SessionError("A verified GitHub user ID is required.")
        if not isinstance(github_login, str) or not 0 < len(github_login) <= 255:
            raise SessionError("A verified GitHub login is required.")
        if refresh_token is None and refresh_token_expires_in is not None:
            raise SessionError("Refresh token expiry requires a refresh token.")
        if refresh_token is not None and (
            type(refresh_token_expires_in) is not int or refresh_token_expires_in < 1
        ):
            raise SessionError("A valid refresh token expiry is required.")
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        INSERT INTO user_sessions (
                            session_id, token_hash, csrf_hash, github_user_id, github_login,
                            encrypted_access_token, encrypted_refresh_token,
                            access_token_expires_at, refresh_token_expires_at,
                            idle_expires_at, absolute_expires_at
                        )
                        VALUES (
                            :session_id, :token_hash, :csrf_hash, :user_id, :login,
                            :access, :refresh,
                            CASE WHEN CAST(:expires_in AS INTEGER) IS NULL THEN NULL
                                 ELSE now() + make_interval(secs => :expires_in) END,
                            CASE WHEN CAST(:refresh_expires_in AS INTEGER) IS NULL THEN NULL
                                 ELSE now() + make_interval(secs => :refresh_expires_in) END,
                            now() + make_interval(secs => :idle),
                            now() + make_interval(secs => :absolute)
                        )
                        RETURNING idle_expires_at
                        """
                    ),
                    {
                        "session_id": uuid4(),
                        "token_hash": _digest(session_token),
                        "csrf_hash": _digest(csrf_token),
                        "user_id": github_user_id,
                        "login": github_login,
                        "access": self._cipher.encrypt(access_token),
                        "refresh": self._cipher.encrypt(refresh_token) if refresh_token else None,
                        "expires_in": access_token_expires_in,
                        "refresh_expires_in": refresh_token_expires_in,
                        "idle": self._idle_ttl.total_seconds(),
                        "absolute": self._absolute_ttl.total_seconds(),
                    },
                )
            ).one()
        return IssuedSession(session_token, csrf_token, row[0])

    async def resolve_session(self, session_token: str) -> AuthenticatedSession | None:
        """Return the session for a live token, extending its idle window."""
        if not _plausible_token(session_token):
            return None
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE user_sessions
                        SET last_seen_at = now(),
                            idle_expires_at = LEAST(
                                now() + make_interval(secs => :idle), absolute_expires_at
                            )
                        WHERE token_hash = :token_hash
                          AND revoked_at IS NULL
                          AND idle_expires_at > now()
                          AND absolute_expires_at > now()
                        RETURNING session_id, github_user_id, github_login, csrf_hash
                        """
                    ),
                    {
                        "token_hash": _digest(session_token),
                        "idle": self._idle_ttl.total_seconds(),
                    },
                )
            ).first()
        if row is None:
            return None
        return AuthenticatedSession(row[0], row[1], row[2], bytes(row[3]))

    @staticmethod
    def csrf_matches(session: AuthenticatedSession, csrf_token: str | None) -> bool:
        """Constant-time CSRF check for state-changing requests."""
        if not _plausible_token(csrf_token):
            return False
        return hmac.compare_digest(session.csrf_hash, _digest(csrf_token))

    async def access_token(
        self,
        session: AuthenticatedSession,
        oauth: GitHubOAuthClient,
    ) -> str:
        """Return a live access token, refreshing and rotating it under a row lock.

        The OAuth client is mandatory so callers cannot accidentally create a
        session path that works only until the first access-token expiry. Failed
        refresh exchanges revoke the local session and erase its unusable token
        material before the failure is returned to the caller.
        """
        refresh_failed = False
        refreshed_access_token: str | None = None
        try:
            async with self._engine.begin() as connection:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT encrypted_access_token, encrypted_refresh_token,
                                   access_token_expires_at, refresh_token_expires_at
                            FROM user_sessions
                            WHERE session_id = :session_id
                              AND revoked_at IS NULL
                              AND idle_expires_at > clock_timestamp()
                              AND absolute_expires_at > clock_timestamp()
                            FOR UPDATE
                            """
                        ),
                        {"session_id": session.session_id},
                    )
                ).first()
                if row is None:
                    raise SessionError("Session is no longer valid.")

                access_token = self._cipher.decrypt(row[0])
                database_now = await _database_time(connection)
                if row[2] is None:
                    raise SessionError("GitHub authorization must be renewed.")
                if row[2] > database_now + timedelta(minutes=2):
                    return access_token
                if row[1] is None or row[3] is None:
                    raise SessionError("GitHub authorization must be renewed.")
                refresh_token = self._cipher.decrypt(row[1])
                refresh_valid = (
                    await connection.execute(
                        text("SELECT :expiry > now() + interval '30 seconds'"),
                        {"expiry": row[3]},
                    )
                ).scalar_one()
                if not refresh_valid:
                    raise SessionError("GitHub authorization must be renewed.")

                try:
                    tokens = await oauth.refresh_access_token(
                        refresh_token=refresh_token
                    )
                except GitHubOAuthConnectError:
                    # A connect-phase failure means the request never reached GitHub.
                    # Keep the token pair so a later request can safely retry.
                    refresh_failed = True
                except GitHubOAuthError:
                    # A read/write failure or provider rejection can happen after
                    # GitHub has consumed the one-time refresh token. Revoke the
                    # local session so callers cannot repeatedly replay a dead pair.
                    await connection.execute(
                        text(
                            """
                            UPDATE user_sessions
                            SET revoked_at = clock_timestamp(),
                                encrypted_access_token = :revoked_access,
                                encrypted_refresh_token = NULL,
                                access_token_expires_at = NULL,
                                refresh_token_expires_at = NULL
                            WHERE session_id = :session_id
                              AND revoked_at IS NULL
                            """
                        ),
                        {
                            "session_id": session.session_id,
                            "revoked_access": self._cipher.encrypt("revoked-session"),
                        },
                    )
                    refresh_failed = True

                if not refresh_failed:
                    if (
                        tokens.refresh_token is None
                        or tokens.refresh_token_expires_in is None
                        or tokens.expires_in is None
                    ):
                        await connection.execute(
                            text(
                                """
                                UPDATE user_sessions
                                SET revoked_at = clock_timestamp(),
                                    encrypted_access_token = :revoked_access,
                                    encrypted_refresh_token = NULL,
                                    access_token_expires_at = NULL,
                                    refresh_token_expires_at = NULL
                                WHERE session_id = :session_id
                                  AND revoked_at IS NULL
                                """
                            ),
                            {
                                "session_id": session.session_id,
                                "revoked_access": self._cipher.encrypt("revoked-session"),
                            },
                        )
                        refresh_failed = True
                    else:
                        # PostgreSQL now() is fixed at transaction start. Recheck
                        # session liveness against wall-clock time after the provider
                        # exchange and before persisting or returning the new pair.
                        session_live = (
                            await connection.execute(
                                text(
                                    """
                                    SELECT revoked_at IS NULL
                                       AND idle_expires_at > clock_timestamp()
                                       AND absolute_expires_at > clock_timestamp()
                                    FROM user_sessions
                                    WHERE session_id = :session_id
                                    """
                                ),
                                {"session_id": session.session_id},
                            )
                        ).scalar_one()
                        if not session_live:
                            raise SessionError("Session is no longer valid.")

                        await connection.execute(
                            text(
                                """
                                UPDATE user_sessions
                                SET encrypted_access_token = :access,
                                    encrypted_refresh_token = :refresh,
                                    access_token_expires_at =
                                        now() + make_interval(secs => :access_ttl),
                                    refresh_token_expires_at =
                                        now() + make_interval(secs => :refresh_ttl)
                                WHERE session_id = :session_id
                                  AND revoked_at IS NULL
                                """
                            ),
                            {
                                "access": self._cipher.encrypt(tokens.access_token),
                                "refresh": self._cipher.encrypt(tokens.refresh_token),
                                "access_ttl": tokens.expires_in,
                                "refresh_ttl": tokens.refresh_token_expires_in,
                                "session_id": session.session_id,
                            },
                        )
                        refreshed_access_token = tokens.access_token
            # Raise only after the revocation transaction commits. Raising inside
            # the transaction would roll back the fail-closed state transition.
            if refresh_failed or refreshed_access_token is None:
                raise SessionError("GitHub authorization must be renewed.")
            return refreshed_access_token
        except TokenCryptoError:
            raise SessionError("Session is no longer valid.") from None

    async def rotate_session(self, session_token: str) -> IssuedSession | None:
        """Replace the session and CSRF tokens; the previous ones stop working at once."""
        if not _plausible_token(session_token):
            return None
        new_session_token = secrets.token_urlsafe(32)
        new_csrf_token = secrets.token_urlsafe(32)
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE user_sessions
                        SET token_hash = :new_token_hash,
                            csrf_hash = :new_csrf_hash,
                            rotated_at = now(),
                            last_seen_at = now()
                        WHERE token_hash = :old_token_hash
                          AND revoked_at IS NULL
                          AND idle_expires_at > now()
                          AND absolute_expires_at > now()
                        RETURNING idle_expires_at
                        """
                    ),
                    {
                        "new_token_hash": _digest(new_session_token),
                        "new_csrf_hash": _digest(new_csrf_token),
                        "old_token_hash": _digest(session_token),
                    },
                )
            ).first()
        if row is None:
            return None
        return IssuedSession(new_session_token, new_csrf_token, row[0])

    async def revoke_session(
        self, session_token: str, oauth: GitHubOAuthClient
    ) -> bool:
        """Revoke one local session, then best-effort revoke its GitHub token."""
        if not _plausible_token(session_token):
            return False
        access_token: str | None = None
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT encrypted_access_token
                        FROM user_sessions
                        WHERE token_hash = :token_hash AND revoked_at IS NULL
                        FOR UPDATE
                        """
                    ),
                    {"token_hash": _digest(session_token)},
                )
            ).first()
            if row is None:
                return False
            try:
                access_token = self._cipher.decrypt(row[0])
            except TokenCryptoError:
                access_token = None
            await connection.execute(
                text(
                    """
                    UPDATE user_sessions
                    SET revoked_at = clock_timestamp(),
                        encrypted_access_token = :revoked_access,
                        encrypted_refresh_token = NULL,
                        access_token_expires_at = NULL,
                        refresh_token_expires_at = NULL
                    WHERE token_hash = :token_hash AND revoked_at IS NULL
                    """
                ),
                {
                    "token_hash": _digest(session_token),
                    "revoked_access": self._cipher.encrypt("revoked-session"),
                },
            )
        # Do not hold a database connection/row lock across the provider request.
        # Local revocation is authoritative even if GitHub is temporarily unavailable.
        if access_token:
            try:
                await oauth.revoke_access_token(access_token=access_token)
            except GitHubOAuthError:
                pass
        return True

    async def revoke_user_sessions(
        self, github_user_id: int, oauth: GitHubOAuthClient
    ) -> int:
        """Revoke all local sessions and the user's GitHub App authorization."""
        access_tokens: list[str] = []
        newly_revoked = 0
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT encrypted_access_token, revoked_at
                        FROM user_sessions
                        WHERE github_user_id = :user_id
                        ORDER BY session_id
                        FOR UPDATE
                        """
                    ),
                    {"user_id": github_user_id},
                )
            ).all()
            for row in rows:
                if row[1] is None:
                    newly_revoked += 1
                try:
                    access_tokens.append(self._cipher.decrypt(row[0]))
                except TokenCryptoError:
                    continue
            result = await connection.execute(
                text(
                    """
                    UPDATE user_sessions
                    SET revoked_at = COALESCE(revoked_at, clock_timestamp()),
                        encrypted_access_token = :revoked_access,
                        encrypted_refresh_token = NULL,
                        access_token_expires_at = NULL,
                        refresh_token_expires_at = NULL
                    WHERE github_user_id = :user_id
                    """
                ),
                {
                    "user_id": github_user_id,
                    "revoked_access": self._cipher.encrypt("revoked-session"),
                },
            )
        # A grant revocation invalidates every access/refresh token for this app
        # and user. One surviving access token is sufficient for the API call.
        for access_token in access_tokens:
            try:
                await oauth.revoke_authorization(access_token=access_token)
                break
            except GitHubOAuthError:
                # The first session's access token may already have expired; try
                # another still-active session before giving up best-effort cleanup.
                continue
        return newly_revoked

