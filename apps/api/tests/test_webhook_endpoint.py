import asyncio
import hashlib
import hmac
from types import SimpleNamespace
from uuid import uuid4

import httpx
from gitwise_api import main
from gitwise_api.config import Settings


class FakeDeliveryStore:
    def __init__(self) -> None:
        self.deliveries: set[str] = set()
        self.processed: set[str] = set()
        self.claim_tokens: dict[str, object] = {}

    async def claim(self, *, delivery_id, event_name, payload):
        key = str(delivery_id)
        outcome = "duplicate" if key in self.processed else (
            "in_progress" if key in self.deliveries else "claimed"
        )
        self.deliveries.add(key)
        token = uuid4()
        self.claim_tokens[key] = token
        return SimpleNamespace(
            delivery_id=delivery_id, outcome=outcome, claim_token=token
        )

    async def mark_processed(self, delivery_id, *, claim_token):
        self.processed.add(str(delivery_id))

    async def mark_failed(self, delivery_id, *, claim_token, error_code):
        self.deliveries.discard(str(delivery_id))


def test_github_ping_endpoint_authenticates_and_deduplicates(monkeypatch) -> None:
    async def exercise() -> None:
        secret = "endpoint-test-secret"
        monkeypatch.setattr(
            main,
            "get_settings",
            lambda: Settings(github_webhook_secret=secret),
        )
        store = FakeDeliveryStore()
        main.app.state.webhook_delivery_store = store
        delivery_id = str(uuid4())
        payload = b'{"zen":"Keep it logically awesome."}'
        signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        headers = {
            "content-type": "application/json",
            "x-github-event": "ping",
            "x-github-delivery": delivery_id,
            "x-hub-signature-256": f"sha256={signature}",
        }
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://gitwise.test",
        ) as client:
            first = await client.post("/webhooks/github", content=payload, headers=headers)
            second = await client.post("/webhooks/github", content=payload, headers=headers)
            assert first.status_code == 200
            assert first.json() == {"status": "accepted", "event": "ping"}
            assert second.status_code == 200
            assert second.json() == {"status": "duplicate", "event": "ping"}

            invalid = await client.post(
                "/webhooks/github",
                content=payload,
                headers={**headers, "x-hub-signature-256": "sha256=" + "0" * 64},
            )
            assert invalid.status_code == 401

            not_configured = await client.post(
                "/webhooks/github",
                content=payload,
                headers={
                    **headers,
                    "x-github-event": "installation",
                    "x-github-delivery": str(uuid4()),
                },
            )
            assert not_configured.status_code == 503

        del main.app.state.webhook_delivery_store

    asyncio.run(exercise())


SECRET = "endpoint-test-secret"
LIFECYCLE_PAYLOAD = b'{"action":"created","installation":{"id":456,"app_id":123}}'


class FakeGitHubAppClient:
    instances: list["FakeGitHubAppClient"] = []

    def __init__(self, *, app_id, private_key) -> None:
        self.app_id = app_id
        self.closed = False
        FakeGitHubAppClient.instances.append(self)

    async def close(self) -> None:
        self.closed = True


def _lifecycle_headers(payload: bytes, *, event: str = "installation") -> dict[str, str]:
    signature = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-github-event": event,
        "x-github-delivery": str(uuid4()),
        "x-hub-signature-256": f"sha256={signature}",
    }


def _configure_lifecycle(monkeypatch, handler) -> FakeDeliveryStore:
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(
            github_webhook_secret=SECRET,
            github_app_id=123,
            github_app_private_key="test-key",
        ),
    )
    monkeypatch.setattr(main, "GitHubAppClient", FakeGitHubAppClient)
    monkeypatch.setattr(main, "process_installation_event", handler)
    FakeGitHubAppClient.instances = []
    store = FakeDeliveryStore()
    main.app.state.webhook_delivery_store = store
    main.app.state.installation_grant_store = object()
    return store


def _cleanup_lifecycle() -> None:
    del main.app.state.webhook_delivery_store
    del main.app.state.installation_grant_store


async def _post(payload: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gitwise.test"
    ) as client:
        return await client.post("/webhooks/github", content=payload, headers=headers)


def test_signed_lifecycle_event_is_processed_then_marked_processed(monkeypatch) -> None:
    calls: list[dict] = []

    async def handler(**kwargs) -> str:
        calls.append(kwargs)
        return "reconciled:2;revoked:0"

    store = _configure_lifecycle(monkeypatch, handler)
    headers = _lifecycle_headers(LIFECYCLE_PAYLOAD)
    try:
        response = asyncio.run(_post(LIFECYCLE_PAYLOAD, headers))
    finally:
        _cleanup_lifecycle()

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "event": "installation"}
    assert len(calls) == 1
    assert calls[0]["event_name"] == "installation"
    assert calls[0]["app_id"] == 123
    assert calls[0]["payload"]["installation"]["id"] == 456
    assert headers["x-github-delivery"] in store.processed
    assert FakeGitHubAppClient.instances[0].closed


def test_unsigned_lifecycle_event_never_reaches_the_handler(monkeypatch) -> None:
    async def handler(**kwargs) -> str:
        raise AssertionError("handler must not run for an unsigned event")

    store = _configure_lifecycle(monkeypatch, handler)
    headers = {
        **_lifecycle_headers(LIFECYCLE_PAYLOAD),
        "x-hub-signature-256": "sha256=" + "0" * 64,
    }
    try:
        response = asyncio.run(_post(LIFECYCLE_PAYLOAD, headers))
    finally:
        _cleanup_lifecycle()

    assert response.status_code == 401
    assert store.deliveries == set()
    assert FakeGitHubAppClient.instances == []


def test_lifecycle_handler_failure_is_retryable_and_not_marked_processed(
    monkeypatch,
) -> None:
    async def handler(**kwargs) -> str:
        raise RuntimeError("GitHub unavailable")

    store = _configure_lifecycle(monkeypatch, handler)
    headers = _lifecycle_headers(LIFECYCLE_PAYLOAD)
    try:
        response = asyncio.run(_post(LIFECYCLE_PAYLOAD, headers))
    finally:
        _cleanup_lifecycle()

    assert response.status_code == 503
    assert headers["x-github-delivery"] not in store.processed
    assert headers["x-github-delivery"] not in store.deliveries
    assert FakeGitHubAppClient.instances[0].closed


def test_invalid_lifecycle_event_is_rejected_without_leaking_detail(monkeypatch) -> None:
    async def handler(**kwargs) -> str:
        raise main.GitHubInstallationEventError("Installation event action is required.")

    store = _configure_lifecycle(monkeypatch, handler)
    headers = _lifecycle_headers(LIFECYCLE_PAYLOAD)
    try:
        response = asyncio.run(_post(LIFECYCLE_PAYLOAD, headers))
    finally:
        _cleanup_lifecycle()

    assert response.status_code == 400
    assert headers["x-github-delivery"] not in store.processed
