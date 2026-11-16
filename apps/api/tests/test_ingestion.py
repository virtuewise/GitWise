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
        "/repos/acme/widget/git/blobs/manifest12": {
            "encoding": "base64",
            "content": "W3Byb2plY3Rd",
            "size": 12,
        },
        "/repos/acme/widget/git/trees/tree1234567": {
            "truncated": False,
            "tree": [
                {
                    "path": "README.md",
                    "type": "blob",
                    "sha": "readme123",
                    "size": 20,
                    "mode": "100644",
                },
                {
                    "path": "apps/api/README.md",
                    "type": "blob",
                    "sha": "nestedreadme",
                    "size": 30,
                    "mode": "100644",
                },
                {
                    "path": "apps/api/pyproject.toml",
                    "type": "blob",
                    "sha": "manifest12",
                    "size": 100,
                    "mode": "100644",
                },
                {"path": "src", "type": "tree", "sha": "directory1"},
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/trees/tree1234567"):
            assert request.url.params.get("recursive") == "1"
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        repository = parse_github_repository_url("https://github.com/acme/widget")
        snapshot = await service.inspect(repository)

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


@pytest.mark.anyio
async def test_inspect_extracts_package_json_dependencies_with_blob_provenance() -> None:
    responses = {
        "/repos/acme/widget": {
            "default_branch": "main",
            "description": None,
            "language": "TypeScript",
            "private": False,
            "visibility": "public",
        },
        "/repos/acme/widget/branches/main": {
            "commit": {
                "sha": "commit7654321",
                "commit": {"tree": {"sha": "tree7654321"}},
            }
        },
        "/repos/acme/widget/git/trees/tree7654321": {
            "truncated": False,
            "tree": [
                {
                    "path": "frontend/package.json",
                    "type": "blob",
                    "sha": "blob7654321",
                    "size": 75,
                    "mode": "100644",
                }
            ],
        },
        "/repos/acme/widget/git/blobs/blob7654321": {
            "encoding": "base64",
            "content": "eyJkZXBlbmRlbmNpZXMiOnsiZXhhbXBsZS1saWIiOiJeMS4yLjAifX0=",
            "size": 43,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        repository = parse_github_repository_url("https://github.com/acme/widget")
        snapshot = await service.inspect(repository)

    dependency_facts = [
        fact for fact in snapshot.facts if fact.fact_type == "dependency.declared"
    ]
    assert len(dependency_facts) == 1
    fact = dependency_facts[0]
    assert fact.value == {
        "name": "example-lib",
        "constraint": "^1.2.0",
        "ecosystem": "npm",
        "group": "dependencies",
    }
    assert fact.evidence[0].source_path == "frontend/package.json"
    assert fact.evidence[0].commit_sha == snapshot.commit_sha
    assert fact.evidence[0].extraction_method == "github_git_blob_manifest_parser"



@pytest.mark.anyio
async def test_inspect_builds_commit_pinned_python_import_graph() -> None:
    import base64

    sources = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "def run():\n    return True\n",
        "src/pkg/service.py": "from .core import run\n",
    }
    blob_shas = {
        "src/pkg/__init__.py": "initblob",
        "src/pkg/core.py": "coreblob",
        "src/pkg/service.py": "serviceblob",
    }
    responses = {
        "/repos/acme/widget": {
            "default_branch": "main",
            "description": None,
            "language": "Python",
            "private": False,
            "visibility": "public",
        },
        "/repos/acme/widget/branches/main": {
            "commit": {
                "sha": "commitgraph123",
                "commit": {"tree": {"sha": "treegraph123"}},
            }
        },
        "/repos/acme/widget/git/trees/treegraph123": {
            "truncated": False,
            "tree": [
                {
                    "path": path,
                    "type": "blob",
                    "sha": blob_shas[path],
                    "size": len(source.encode("utf-8")),
                    "mode": "100644",
                }
                for path, source in sources.items()
            ],
        },
    }
    for path, source in sources.items():
        raw = source.encode("utf-8")
        responses[f"/repos/acme/widget/git/blobs/{blob_shas[path]}"] = {
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
            "size": len(raw),
        }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        repository = parse_github_repository_url("https://github.com/acme/widget")
        snapshot = await service.inspect(repository)

    assert {node.module_name for node in snapshot.architecture_nodes} == {
        "pkg",
        "pkg.core",
        "pkg.service",
    }
    assert snapshot.architecture_analysis.status == "complete"
    assert snapshot.architecture_analysis.files_discovered == 3
    assert snapshot.architecture_analysis.files_selected == 3
    assert snapshot.architecture_analysis.files_analyzed == 3
    assert snapshot.architecture_analysis.limitations == []
    assert len(snapshot.architecture_edges) == 1
    edge = snapshot.architecture_edges[0]
    assert edge.source_module == "pkg.service"
    assert edge.target_module == "pkg.core"
    assert edge.source_path == "src/pkg/service.py"
    assert edge.line_number == 1
    assert edge.commit_sha == snapshot.commit_sha
    assert edge.extraction_method == "python_ast_import_analyzer"


@pytest.mark.anyio
async def test_inspect_marks_architecture_partial_when_blob_provenance_is_inconsistent() -> None:
    import base64

    source = b"from .core import run\n"
    responses = {
        "/repos/acme/widget": {
            "default_branch": "main",
            "description": None,
            "language": "Python",
            "private": False,
            "visibility": "public",
        },
        "/repos/acme/widget/branches/main": {
            "commit": {
                "sha": "commitpartial123",
                "commit": {"tree": {"sha": "treepartial123"}},
            }
        },
        "/repos/acme/widget/git/trees/treepartial123": {
            "truncated": False,
            "tree": [
                {
                    "path": "src/pkg/service.py",
                    "type": "blob",
                    "sha": "serviceblob",
                    "size": len(source),
                    "mode": "100644",
                }
            ],
        },
        "/repos/acme/widget/git/blobs/serviceblob": {
            "encoding": "base64",
            "content": base64.b64encode(source).decode("ascii"),
            "size": len(source) + 1,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        snapshot = await service.inspect(
            parse_github_repository_url("https://github.com/acme/widget")
        )

    assert snapshot.architecture_analysis.status == "partial"
    assert snapshot.architecture_analysis.files_discovered == 1
    assert snapshot.architecture_analysis.files_selected == 1
    assert snapshot.architecture_analysis.files_analyzed == 0
    assert "python_blob_size_mismatch" in snapshot.architecture_analysis.limitations


@pytest.mark.anyio
async def test_inspect_resolves_an_explicit_commit_ref() -> None:
    responses = {
        "/repos/acme/widget": {
            "default_branch": "main",
            "description": None,
            "language": "Python",
            "private": False,
            "visibility": "public",
        },
        "/repos/acme/widget/commits/oldercommit123": {
            "sha": "oldercommit123",
            "commit": {"tree": {"sha": "oldertree123"}},
        },
        "/repos/acme/widget/git/trees/oldertree123": {
            "truncated": False,
            "tree": [],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        snapshot = await service.inspect(
            parse_github_repository_url("https://github.com/acme/widget"),
            ref="oldercommit123",
        )

    assert snapshot.commit_sha == "oldercommit123"
    assert snapshot.tree_sha == "oldertree123"
    assert snapshot.requested_ref == "oldercommit123"
    assert snapshot.architecture_analysis.status == "complete"


@pytest.mark.anyio
async def test_invalid_python_syntax_makes_architecture_analysis_partial() -> None:
    import base64

    source = b"def broken(:\n"
    responses = {
        "/repos/acme/widget": {
            "default_branch": "main",
            "description": None,
            "language": "Python",
            "private": False,
            "visibility": "public",
        },
        "/repos/acme/widget/branches/main": {
            "commit": {
                "sha": "commitinvalid123",
                "commit": {"tree": {"sha": "treeinvalid123"}},
            }
        },
        "/repos/acme/widget/git/trees/treeinvalid123": {
            "truncated": False,
            "tree": [
                {
                    "path": "src/pkg/broken.py",
                    "type": "blob",
                    "sha": "brokenblob",
                    "size": len(source),
                    "mode": "100644",
                }
            ],
        },
        "/repos/acme/widget/git/blobs/brokenblob": {
            "encoding": "base64",
            "content": base64.b64encode(source).decode("ascii"),
            "size": len(source),
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        snapshot = await service.inspect(
            parse_github_repository_url("https://github.com/acme/widget")
        )

    assert snapshot.architecture_analysis.status == "partial"
    assert snapshot.architecture_analysis.files_discovered == 1
    assert snapshot.architecture_analysis.files_selected == 1
    assert snapshot.architecture_analysis.files_analyzed == 0
    assert "invalid_python_syntax" in snapshot.architecture_analysis.limitations


@pytest.mark.anyio
async def test_github_response_body_is_capped_even_without_trustworthy_content_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"payload":"' + b"x" * 100 + b'"}',
            headers={"Content-Length": "1"},
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        service = GitHubRepositoryClient(http_client)
        with pytest.raises(HTTPException) as error:
            await service._get("/oversized", max_response_bytes=32)

    assert error.value.status_code == 502
    assert "size limit" in error.value.detail
