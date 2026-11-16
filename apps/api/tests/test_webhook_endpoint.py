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

            unsupported = await client.post(
                "/webhooks/github",
                content=payload,
                headers={**headers, "x-github-event": "installation"},
            )
            assert unsupported.status_code == 501

        del main.app.state.webhook_delivery_store

    asyncio.run(exercise())
