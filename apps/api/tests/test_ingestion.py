from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from gitwise_api.ingestion import GitHubRepositoryClient
from gitwise_api.repositories import parse_github_repository_url


@pytest.mark.anyio
async def test_inspect_builds_commit_pinned_genome_snapshot() -> None:
    responses = {
        "/repos/acme/widget": {
            "default_branch": "main",
            "description": "A sample project",
            "language": "Python",
            "private": False,
            "visibility": "public",
        },
        "/repos/acme/widget/branches/main": {
            "commit": {
                "sha": "commit1234567",
                "commit": {"tree": {"sha": "tree1234567"}},
            }
        },
        "/repos/acme/widget/git/trees/tree1234567": {
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob", "sha": "readme123", "size": 20, "mode": "100644"},
                {"path": "apps/api/README.md", "type": "blob", "sha": "nestedreadme", "size": 30, "mode": "100644"},
                {"path": "apps/api/pyproject.toml", "type": "blob", "sha": "manifest12", "size": 100, "mode": "100644"},
                {"path": "src", "type": "tree", "sha": "directory1"},
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("recursive") == "1" if request.url.path.endswith("/git/trees/tree1234567") else True
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        snapshot = await service.inspect(parse_github_repository_url("https://github.com/acme/widget"))

    assert snapshot.repository == "acme/widget"
    assert snapshot.commit_sha == "commit1234567"
    assert snapshot.files_analyzed == 3
    assert "apps/api/pyproject.toml" in {entry.path for entry in snapshot.tree}
    assert snapshot.truncated is False
    assert {fact.fact_type for fact in snapshot.facts} >= {
        "repository.default_branch",
        "repository.primary_language",
        "project.manifest",
        "project.documentation_entrypoint",
    }
    assert any(
        fact.fact_type == "project.manifest"
        and fact.value["path"] == "apps/api/pyproject.toml"
        for fact in snapshot.facts
    )
    assert all(
        evidence.commit_sha == snapshot.commit_sha
        for fact in snapshot.facts
        for evidence in fact.evidence
    )


@pytest.mark.anyio
async def test_inspect_maps_missing_repository_to_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        with pytest.raises(HTTPException) as error:
            await service.inspect(parse_github_repository_url("https://github.com/acme/missing"))

    assert error.value.status_code == 404


@pytest.mark.anyio
async def test_inspect_maps_rate_limit_to_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Retry-After": "30"}, json={"message": "rate limited"})

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        with pytest.raises(HTTPException) as error:
            await service.inspect(parse_github_repository_url("https://github.com/acme/widget"))

    assert error.value.status_code == 503
    assert error.value.headers["Retry-After"] == "30"
