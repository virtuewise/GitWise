import httpx
import pytest

from gitwise_api import main
from gitwise_api.genome import SoftwareGenomeSnapshot


class FakeGitHubClient:
    def __init__(self) -> None:
        self.closed = False
        self.requested_refs: list[str | None] = []

    async def inspect(self, repository, ref=None):
        self.requested_refs.append(ref)
        commit = f"{ref}-commit123" if ref else "defaultcommit123"
        return SoftwareGenomeSnapshot(
            repository=repository.full_name,
            repository_url=repository.canonical_url,
            default_branch="main",
            requested_ref=ref or "main",
            commit_sha=commit,
            tree_sha=f"{ref or 'default'}-tree123",
            is_private=False,
        )

    async def close(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_inspect_endpoint_passes_explicit_ref_to_ingestion(monkeypatch) -> None:
    fake = FakeGitHubClient()
    monkeypatch.setattr(main, "GitHubRepositoryClient", lambda: fake)
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/repositories/inspect",
            json={"url": "https://github.com/acme/widget", "ref": "release-1.0"},
        )

    assert response.status_code == 200
    assert response.json()["requested_ref"] == "release-1.0"
    assert response.json()["commit_sha"] == "release-1.0-commit123"
    assert fake.requested_refs == ["release-1.0"]
    assert fake.closed is True


@pytest.mark.anyio
async def test_compare_endpoint_compares_two_explicit_refs(monkeypatch) -> None:
    fake = FakeGitHubClient()
    monkeypatch.setattr(main, "GitHubRepositoryClient", lambda: fake)
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/repositories/compare",
            json={
                "url": "https://github.com/acme/widget",
                "base_ref": "v1.0.0",
                "head_ref": "v2.0.0",
            },
        )

    assert response.status_code == 200
    result = response.json()
    assert result["repository"] == "acme/widget"
    assert result["base_commit_sha"] == "v1.0.0-commit123"
    assert result["head_commit_sha"] == "v2.0.0-commit123"
    assert result["changes"] == []
    assert sorted(fake.requested_refs) == ["v1.0.0", "v2.0.0"]
    assert fake.closed is True


@pytest.mark.anyio
async def test_compare_endpoint_rejects_whitespace_in_refs() -> None:
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/repositories/compare",
            json={
                "url": "https://github.com/acme/widget",
                "base_ref": "main branch",
                "head_ref": "main",
            },
        )

    assert response.status_code == 422
