import asyncio

from fastapi import FastAPI

from gitwise_api.config import get_settings
from gitwise_api.evolution import GenomeComparison, compare_genomes
from gitwise_api.genome import SoftwareGenomeSnapshot
from gitwise_api.ingestion import GitHubRepositoryClient
from gitwise_api.repositories import (
    RepositoryCompareRequest,
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
