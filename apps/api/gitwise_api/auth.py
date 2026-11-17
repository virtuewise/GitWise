"""GitHub App user sign-in, session cookies and CSRF protection.

Identity comes only from a server-side session created after GitHub verified the
user. No route here (or any future route) may accept a user or installation ID
from the request as proof of identity.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from gitwise_api.github_oauth import GitHubOAuthClient, GitHubOAuthError
from gitwise_api.github_user import GitHubUserAuthenticationError, GitHubUserClient
from gitwise_api.sessions import (
    AuthenticatedSession,
    AuthorizationStateError,
    IssuedSession,
    SessionError,
    SessionStore,
)

SESSION_COOKIE = "gitwise_session"
CSRF_COOKIE = "gitwise_csrf"
STATE_COOKIE = "gitwise_oauth_state"
STATE_COOKIE_PATH = "/auth/github"
STATE_COOKIE_MAX_AGE = 600
CSRF_HEADER = "x-csrf-token"
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(prefix="/auth", tags=["auth"])


@dataclass
class AuthComponents:
    sessions: SessionStore
    oauth: GitHubOAuthClient
    public_base_url: str
    user_client_factory: Callable[[str], GitHubUserClient] = GitHubUserClient

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")


def _components(request: Request) -> AuthComponents:
    components = getattr(request.app.state, "auth_components", None)
    if components is None:
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured.")
    return components


def _set_session_cookies(
    response: Response, components: AuthComponents, issued: IssuedSession
) -> None:
    # The session token is invisible to scripts. The CSRF token must be readable so the
    # web app can echo it in a header; it only proves intent and grants nothing alone.
    response.set_cookie(
        SESSION_COOKIE,
        issued.session_token,
        httponly=True,
        secure=components.secure_cookies,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf_token,
        httponly=False,
        secure=components.secure_cookies,
        samesite="strict",
        path="/",
    )


def _clear_session_cookies(response: Response, components: AuthComponents) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name, path="/", secure=components.secure_cookies, samesite="lax"
        )


def _sign_in_failure(components: AuthComponents) -> JSONResponse:
    response = JSONResponse(
        status_code=400,
        content={"detail": "GitHub sign-in could not be completed."},
        headers=NO_STORE,
    )
    response.delete_cookie(
        STATE_COOKIE,
        path=STATE_COOKIE_PATH,
        secure=components.secure_cookies,
        samesite="lax",
    )
    return response


@router.get("/github/login")
async def begin_github_login(request: Request) -> RedirectResponse:
    components = _components(request)
    start = await components.sessions.begin_authorization()
    response = RedirectResponse(
        components.oauth.authorization_url(
            state=start.state, code_challenge=start.code_challenge
        ),
        status_code=302,
        headers=NO_STORE,
    )
    # Binds the one-time state to this browser, so a state value an attacker obtained
    # cannot be used to sign a victim into the attacker's account.
    response.set_cookie(
        STATE_COOKIE,
        start.state,
        max_age=STATE_COOKIE_MAX_AGE,
        httponly=True,
        secure=components.secure_cookies,
        samesite="lax",
        path=STATE_COOKIE_PATH,
    )
    return response


@router.get("/github/callback")
async def complete_github_login(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    components = _components(request)
    cookie_state = request.cookies.get(STATE_COOKIE)
    if (
        error
        or not code
        or not state
        or not cookie_state
        or not hmac.compare_digest(state.encode(), cookie_state.encode())
    ):
        return _sign_in_failure(components)

    try:
        verifier = await components.sessions.consume_authorization(state)
    except AuthorizationStateError:
        return _sign_in_failure(components)

    user_client = None
    try:
        tokens = await components.oauth.exchange_code(code=code, code_verifier=verifier)
        user_client = components.user_client_factory(tokens.access_token)
        user = await user_client.verify_user()
    except (GitHubOAuthError, GitHubUserAuthenticationError, httpx.HTTPError):
        return _sign_in_failure(components)
    finally:
        if user_client is not None:
            await user_client.close()

    try:
        issued = await components.sessions.create_session(
            github_user_id=user.user_id,
            github_login=user.login,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            access_token_expires_in=tokens.expires_in,
            refresh_token_expires_in=tokens.refresh_token_expires_in,
        )
    except SessionError:
        return _sign_in_failure(components)

    # The destination is fixed server configuration; it is never taken from the request.
    response = RedirectResponse(
        f"{components.public_base_url.rstrip('/')}/", status_code=303, headers=NO_STORE
    )
    _set_session_cookies(response, components, issued)
    response.delete_cookie(
        STATE_COOKIE,
        path=STATE_COOKIE_PATH,
        secure=components.secure_cookies,
        samesite="lax",
    )
    return response


async def current_session(request: Request) -> AuthenticatedSession:
    """Resolve the caller from the session cookie alone."""
    components = _components(request)
    token = request.cookies.get(SESSION_COOKIE)
    session = await components.sessions.resolve_session(token) if token else None
    if session is None:
        raise HTTPException(
            status_code=401, detail="Authentication required.", headers=NO_STORE
        )
    return session


async def csrf_protected_session(
    request: Request, session: AuthenticatedSession = Depends(current_session)
) -> AuthenticatedSession:
    """Require the CSRF header on top of a valid session for state-changing requests."""
    if not SessionStore.csrf_matches(session, request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.", headers=NO_STORE)
    return session


@router.get("/session")
async def read_session(
    session: AuthenticatedSession = Depends(current_session),
) -> JSONResponse:
    return JSONResponse(
        {"user_id": session.github_user_id, "login": session.github_login},
        headers=NO_STORE,
    )


@router.post("/session/rotate", status_code=204)
async def rotate_session(
    request: Request, _: AuthenticatedSession = Depends(csrf_protected_session)
) -> Response:
    components = _components(request)
    issued = await components.sessions.rotate_session(request.cookies[SESSION_COOKIE])
    if issued is None:
        raise HTTPException(
            status_code=401, detail="Authentication required.", headers=NO_STORE
        )
    response = Response(status_code=204, headers=NO_STORE)
    _set_session_cookies(response, components, issued)
    return response


@router.post("/logout", status_code=204)
async def logout(
    request: Request, _: AuthenticatedSession = Depends(csrf_protected_session)
) -> Response:
    components = _components(request)
    await components.sessions.revoke_session(request.cookies[SESSION_COOKIE], components.oauth)
    response = Response(status_code=204, headers=NO_STORE)
    _clear_session_cookies(response, components)
    return response


@router.post("/logout/all", status_code=204)
async def logout_everywhere(
    request: Request, session: AuthenticatedSession = Depends(csrf_protected_session)
) -> Response:
    components = _components(request)
    await components.sessions.revoke_user_sessions(session.github_user_id, components.oauth)
    response = Response(status_code=204, headers=NO_STORE)
    _clear_session_cookies(response, components)
    return response
