"""Durable idempotency primitives for verified GitHub webhook deliveries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class WebhookDeliveryConflict(RuntimeError):
    """A delivery ID was reused with different event content."""


@dataclass(frozen=True)
class WebhookDeliveryResult:
    delivery_id: UUID
    outcome: str


class GitHubWebhookDeliveryStore:
    """Atomically claim verified delivery IDs before dispatching their events.

    Signature verification must happen before calling this store. A successful
    claim is durable and unique across workers; duplicate deliveries are not
    eligible for a second dispatch. Retention cleanup is an explicit operator
    policy and must not run while deliveries are still within the replay window.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> GitHubWebhookDeliveryStore:
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("GITWISE_DATABASE_URL must use postgresql+asyncpg.")
        return cls(create_async_engine(database_url, pool_pre_ping=True))

    async def close(self) -> None:
        await self._engine.dispose()

    async def claim(
        self,
        *,
        delivery_id: UUID,
        event_name: str,
        payload: bytes,
    ) -> WebhookDeliveryResult:
        if not isinstance(delivery_id, UUID):
            raise TypeError("GitHub delivery ID must be a parsed UUID.")
        if not event_name or len(event_name) > 100:
            raise ValueError("A valid GitHub event name is required.")
        if not isinstance(payload, bytes):
            raise TypeError("Webhook payload must be the original raw request bytes.")

        digest = hashlib.sha256(payload).hexdigest()
        async with self._engine.begin() as connection:
            inserted = await connection.execute(
                text("""
                    INSERT INTO github_webhook_deliveries (
                        delivery_id, event_name, payload_sha256
                    ) VALUES (:delivery_id, :event_name, :digest)
                    ON CONFLICT (delivery_id) DO NOTHING
                    RETURNING delivery_id
                """),
                {
                    "delivery_id": str(delivery_id),
                    "event_name": event_name,
                    "digest": digest,
                },
            )
            if inserted.scalar_one_or_none() is not None:
                return WebhookDeliveryResult(delivery_id, "claimed")

            existing = await connection.execute(
                text("""
                    SELECT event_name, payload_sha256
                    FROM github_webhook_deliveries
                    WHERE delivery_id = :delivery_id
                """),
                {"delivery_id": str(delivery_id)},
            )
            row = existing.mappings().one()
            if row["event_name"] != event_name or row["payload_sha256"].strip() != digest:
                raise WebhookDeliveryConflict(
                    "GitHub delivery ID was previously recorded with different content."
                )

        return WebhookDeliveryResult(delivery_id, "duplicate")
