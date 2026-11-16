import asyncio
import os
from uuid import uuid4

import pytest
from gitwise_api.webhook_deliveries import (
    GitHubWebhookDeliveryStore,
    WebhookDeliveryConflict,
)

DATABASE_URL = os.getenv("GITWISE_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration database is not configured")
def test_postgresql_webhook_delivery_claim_is_idempotent_and_detects_reuse() -> None:
    async def exercise() -> None:
        from sqlalchemy import text

        delivery_id = uuid4()
        store = GitHubWebhookDeliveryStore.from_url(DATABASE_URL)
        try:
            first = await store.claim(
                delivery_id=delivery_id,
                event_name="installation",
                payload=b'{"action":"created"}',
            )
            concurrent = await store.claim(
                delivery_id=delivery_id,
                event_name="installation",
                payload=b'{"action":"created"}',
            )
            assert first.outcome == "claimed"
            assert first.attempt_count == 1
            assert concurrent.outcome == "in_progress"

            await store.mark_failed(
                delivery_id,
                claim_token=first.claim_token,
                error_code="temporary_failure",
            )
            retry = await store.claim(
                delivery_id=delivery_id,
                event_name="installation",
                payload=b'{"action":"created"}',
            )
            assert retry.outcome == "claimed"
            assert retry.attempt_count == 2

            with pytest.raises(RuntimeError, match="stale or no longer active"):
                await store.mark_processed(delivery_id, claim_token=first.claim_token)

            await store.mark_processed(delivery_id, claim_token=retry.claim_token)
            duplicate = await store.claim(
                delivery_id=delivery_id,
                event_name="installation",
                payload=b'{"action":"created"}',
            )
            assert duplicate.outcome == "duplicate"
            assert duplicate.attempt_count == 2

            with pytest.raises(WebhookDeliveryConflict):
                await store.claim(
                    delivery_id=delivery_id,
                    event_name="installation",
                    payload=b'{"action":"deleted"}',
                )
        finally:
            async with store._engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM github_webhook_deliveries WHERE delivery_id = :id"),
                    {"id": str(delivery_id)},
                )
            await store.close()

    asyncio.run(exercise())
