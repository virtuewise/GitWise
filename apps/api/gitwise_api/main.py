from fastapi import FastAPI

from gitwise_api.config import get_settings
from gitwise_api.genome import SoftwareGenomeSnapshot
from gitwise_api.ingestion import GitHubRepositoryClient
from gitwise_api.repositories import (
    RepositoryInspectRequest,
    RepositoryReference,
    parse_github_repository_url,
)

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="Repository intelligence and Software Genome services for GitWise.",
    version="0.1.0",
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
        return await github.inspect(repository)
    finally:
        await github.close()
