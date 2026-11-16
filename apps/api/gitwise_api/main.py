import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request

from gitwise_api.config import get_settings
from gitwise_api.evolution import GenomeComparison, compare_genomes
from gitwise_api.genome import SoftwareGenomeSnapshot
from gitwise_api.github_app import GitHubAppClient
from gitwise_api.github_installation_events import (
    GitHubInstallationEventError,
    process_installation_event,
)
from gitwise_api.ingestion import GitHubRepositoryClient
from gitwise_api.installation_grants import InstallationGrantStore
from gitwise_api.repositories import (
    RepositoryCompareRequest,
    RepositoryInspectRequest,
    RepositoryReference,
    parse_github_repository_url,
)
from gitwise_api.webhook_deliveries import (
    GitHubWebhookDeliveryStore,
    WebhookDeliveryConflict,
)
from gitwise_api.webhooks import verify_github_webhook_signature

MAX_WEBHOOK_BODY_BYTES = 1_000_000


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    application.state.webhook_delivery_store = (
        GitHubWebhookDeliveryStore.from_url(settings.database_url)
        if settings.database_url
        else None
    )
    application.state.installation_grant_store = (
        InstallationGrantStore.from_url(settings.database_url)
        if settings.database_url
        else None
    )
    try:
        yield
    finally:
        delivery_store = getattr(application.state, "webhook_delivery_store", None)
        grant_store = getattr(application.state, "installation_grant_store", None)
        if delivery_store is not None:
            await delivery_store.close()
        if grant_store is not None:
            await grant_store.close()


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="Repository intelligence and Software Genome services for GitWise.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["operations"])
async def health_check() -> dict[str, str]:
    """Return process health without exposing configuration or secrets."""
    return {"status": "ok", "service": "gitwise-api"}


@app.get("/", tags=["operations"])
async def root() -> dict[str, str]:
    return {
        "name": "GitWise",
        "message": "The intelligence layer for software development.",
        "docs": "/docs",
    }


@app.post("/webhooks/github", tags=["webhooks"])
async def receive_github_webhook(request: Request) -> dict[str, str]:
    """Authenticate and durably deduplicate GitHub's ping handshake.

    Installation lifecycle events are processed only after signature verification,
    App ownership checks, and immutable repository identity verification.
    """
    secret = get_settings().github_webhook_secret
    delivery_store = getattr(request.app.state, "webhook_delivery_store", None)
    if not secret or delivery_store is None:
        raise HTTPException(
            status_code=503,
            detail="GitHub webhook processing is not configured.",
        )

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large.")
    payload = bytes(body)

    if not verify_github_webhook_signature(
        payload,
        request.headers.get("x-hub-signature-256"),
        secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature.")

    event_name = request.headers.get("x-github-event", "")
    if event_name not in ("ping", "installation", "installation_repositories"):
        raise HTTPException(
            status_code=501,
            detail="This GitHub webhook event is not supported yet.",
        )

    delivery_header = request.headers.get("x-github-delivery", "")
    try:
        delivery_id = UUID(delivery_header)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid GitHub delivery ID.") from exc

    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Webhook body must be valid JSON.") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=400, detail="Webhook body must be a JSON object.")

    try:
        result = await delivery_store.claim(
            delivery_id=delivery_id,
            event_name=event_name,
            payload=payload,
        )
    except WebhookDeliveryConflict as exc:
        raise HTTPException(status_code=409, detail="Conflicting webhook delivery ID.") from exc

    if result.outcome == "duplicate":
        return {"status": "duplicate", "event": event_name}
    if result.outcome == "in_progress":
        return {"status": "in_progress", "event": event_name}

    app_id = get_settings().github_app_id
    private_key = get_settings().github_app_private_key
    grant_store = getattr(request.app.state, "installation_grant_store", None)
    if event_name != "ping" and (
        app_id is None or not private_key or grant_store is None
    ):
        try:
            await delivery_store.mark_failed(
                delivery_id,
                claim_token=result.claim_token,
                error_code="app_not_configured",
            )
        except RuntimeError:
            pass
        raise HTTPException(
            status_code=503,
            detail="GitHub App lifecycle processing is not configured.",
        )

    github_app = (
        GitHubAppClient(app_id=app_id, private_key=private_key)
        if event_name != "ping"
        else None
    )
    try:
        if event_name != "ping":
            await process_installation_event(
                event_name=event_name,
                payload=decoded,
                app_id=app_id,
                app=github_app,
                grants=grant_store,
            )
        await delivery_store.mark_processed(
            delivery_id, claim_token=result.claim_token
        )
    except GitHubInstallationEventError as exc:
        try:
            await delivery_store.mark_failed(
                delivery_id,
                claim_token=result.claim_token,
                error_code="invalid_installation_event",
            )
        except RuntimeError:
            pass
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        try:
            await delivery_store.mark_failed(
                delivery_id,
                claim_token=result.claim_token,
                error_code="handler_failure",
            )
        except RuntimeError:
            pass
        raise HTTPException(
            status_code=503,
            detail="Webhook event processing failed and can be retried.",
        ) from exc
    finally:
        if github_app is not None:
            await github_app.close()

    return {"status": "accepted", "event": event_name}


@app.post(
    "/repositories/resolve",
    response_model=RepositoryReference,
    tags=["repositories"],
)
async def resolve_repository(
    request: RepositoryInspectRequest,
) -> RepositoryReference:
    """Validate and normalize a GitHub repository URL without network access."""
    return parse_github_repository_url(request.url)


@app.post(
    "/repositories/inspect",
    response_model=SoftwareGenomeSnapshot,
    tags=["repositories"],
)
async def inspect_repository(request: RepositoryInspectRequest) -> SoftwareGenomeSnapshot:
    """Create a bounded, read-only Software Genome snapshot of a public repository."""
    repository = parse_github_repository_url(request.url)
    github = GitHubRepositoryClient()
    try:
        return await github.inspect(repository, request.ref)
    finally:
        await github.close()


@app.post(
    "/repositories/compare",
    response_model=GenomeComparison,
    tags=["repositories"],
)
async def compare_repository_refs(request: RepositoryCompareRequest) -> GenomeComparison:
    """Compare two explicit refs using fresh, commit-pinned Genome observations."""
    repository = parse_github_repository_url(request.url)
    github = GitHubRepositoryClient()
    try:
        results = await asyncio.gather(
            github.inspect(repository, request.base_ref),
            github.inspect(repository, request.head_ref),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                raise result
        base, head = results
        return compare_genomes(base, head)
    finally:
        await github.close()
