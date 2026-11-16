from fastapi.testclient import TestClient

from gitwise_api.main import app

client = TestClient(app)


def test_health_check() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "gitwise-api"}


def test_root_endpoint() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["name"] == "GitWise"
    assert response.json()["docs"] == "/docs"


def test_resolve_github_repository() -> None:
    response = client.post(
        "/repositories/resolve",
        json={"url": "https://github.com/virtuewise/GitWise.git"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "owner": "virtuewise",
        "name": "GitWise",
        "full_name": "virtuewise/GitWise",
        "canonical_url": "https://github.com/virtuewise/GitWise",
    }


def test_reject_non_github_host() -> None:
    response = client.post(
        "/repositories/resolve",
        json={"url": "https://example.com/owner/repo"},
    )

    assert response.status_code == 422


def test_reject_nested_repository_path() -> None:
    response = client.post(
        "/repositories/resolve",
        json={"url": "https://github.com/owner/repo/tree/main"},
    )

    assert response.status_code == 422
