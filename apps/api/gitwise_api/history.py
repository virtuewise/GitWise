"""Authenticated, read-only access to stored Software Genome history.

The caller is the user named by the server-side session. GitHub is asked who that
user is and which installations they can reach on every request, so a user who
loses access, or whose token is revoked, loses history access immediately. Nothing
in the URL, query string, headers or body is ever treated as an identity.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from gitwise_api.auth import NO_STORE, _components, current_session
from gitwise_api.genome import SoftwareGenomeSnapshot
from gitwise_api.github_user import GitHubUserAuthenticationError, VerifiedGitHubUser
from gitwise_api.sessions import AuthenticatedSession, SessionError
from gitwise_api.storage import GenomeSnapshotStore, SnapshotAccessDenied

router = APIRouter(tags=["history"])
PRIVATE_NO_STORE = {"Cache-Control": "private, no-store"}
MAX_SNAPSHOT_KEY_LENGTH = 512


def _snapshot_store(request: Request) -> GenomeSnapshotStore:
    store = getattr(request.app.state, "genome_snapshot_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Genome history is not configured.", headers=NO_STORE
        )
    return store


def _not_found() -> HTTPException:
    # Missing, unreadable and revoked are deliberately indistinguishable.
    return HTTPException(
        status_code=404, detail="Software Genome snapshot not found.", headers=NO_STORE
    )


async def verified_user(
    request: Request, session: AuthenticatedSession = Depends(current_session)
) -> VerifiedGitHubUser:
    """Re-verify the session's user and installation membership with GitHub."""
    components = _components(request)
    unauthenticated = HTTPException(
        status_code=401, detail="Authentication required.", headers=NO_STORE
    )
    try:
        token = await components.sessions.access_token(session)
    except SessionError:
        raise unauthenticated from None

    client = components.user_client_factory(token)
    try:
        user = await client.verify_user()
    except GitHubUserAuthenticationError:
        raise unauthenticated from None
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503, detail="GitHub could not be reached.", headers=NO_STORE
        ) from None
    finally:
        await client.close()

    if user.user_id != session.github_user_id:
        # The token no longer belongs to the user who signed in; fail closed.
        await components.sessions.revoke_user_sessions(session.github_user_id)
        raise unauthenticated
    return user


@router.get("/repositories/{provider_repository_id}/snapshots")
async def list_repository_snapshots(
    provider_repository_id: int,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    user: VerifiedGitHubUser = Depends(verified_user),
) -> JSONResponse:
    if provider_repository_id < 1:
        raise _not_found()
    try:
        summaries = await _snapshot_store(request).list_snapshots_for_user(
            provider_repository_id, user=user, limit=limit
        )
    except SnapshotAccessDenied:
        raise _not_found() from None
    return JSONResponse(
        {
            "snapshots": [
                {
                    "snapshot_key": item.snapshot_key,
                    "commit_sha": item.commit_sha,
                    "schema_version": item.schema_version,
                    "observed_at": item.observed_at.isoformat(),
                    "stored_at": item.stored_at.isoformat(),
                }
                for item in summaries
            ]
        },
        headers=PRIVATE_NO_STORE,
    )


@router.get("/snapshots/{snapshot_key}", response_model=SoftwareGenomeSnapshot)
async def read_snapshot(
    snapshot_key: str,
    request: Request,
    user: VerifiedGitHubUser = Depends(verified_user),
) -> JSONResponse:
    if not snapshot_key or len(snapshot_key) > MAX_SNAPSHOT_KEY_LENGTH:
        raise _not_found()
    try:
        snapshot = await _snapshot_store(request).get_snapshot_for_user(
            snapshot_key, user=user
        )
    except SnapshotAccessDenied:
        raise _not_found() from None
    return JSONResponse(snapshot.model_dump(mode="json"), headers=PRIVATE_NO_STORE)
