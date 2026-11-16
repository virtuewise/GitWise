"""Durable, recoverable processing for verified GitHub webhook deliveries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class WebhookDeliveryConflict(RuntimeError):
    """A delivery ID was reused with different event content."""


@dataclass(frozen=True)
class WebhookDeliveryResult:
    delivery_id: UUID
    outcome: str
    attempt_count: int = 0
    claim_token: UUID | None = None


class GitHubWebhookDeliveryStore:
    """Claim verified deliveries with expiring leases and explicit completion.

    A delivery is not considered complete when first received. Workers must
    call mark_processed only after all event effects commit, or mark_failed
    after a recoverable failure. Expired processing leases can be reclaimed.
    """

    LEASE_SECONDS = 300

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
            await connection.execute(
                text("""
                    INSERT INTO github_webhook_deliveries (
                        delivery_id, event_name, payload_sha256
                    ) VALUES (:delivery_id, :event_name, :digest)
                    ON CONFLICT (delivery_id) DO NOTHING
                """),
                {
                    "delivery_id": str(delivery_id),
                    "event_name": event_name,
                    "digest": digest,
                },
            )
            existing = await connection.execute(
                text("""
                    SELECT event_name, payload_sha256, processing_status,
                           attempt_count, lease_expires_at
                    FROM github_webhook_deliveries
                    WHERE delivery_id = :delivery_id
                    FOR UPDATE
                """),
                {"delivery_id": str(delivery_id)},
            )
            row = existing.mappings().one()
            if row["event_name"] != event_name or row["payload_sha256"].strip() != digest:
                raise WebhookDeliveryConflict(
                    "GitHub delivery ID was previously recorded with different content."
                )

            status = row["processing_status"]
            if status == "processed":
                return WebhookDeliveryResult(delivery_id, "duplicate", row["attempt_count"])
            if status == "processing" and row["lease_expires_at"] is not None:
                lease_active = await connection.scalar(
                    text("SELECT :lease_expires_at > now()"),
                    {"lease_expires_at": row["lease_expires_at"]},
                )
                if lease_active:
                    return WebhookDeliveryResult(
                        delivery_id, "in_progress", row["attempt_count"]
                    )

            claim_token = uuid4()
            updated = await connection.execute(
                text("""
                    UPDATE github_webhook_deliveries
                    SET processing_status = 'processing',
                        attempt_count = attempt_count + 1,
                        lease_expires_at = now() + make_interval(secs => :lease_seconds),
                        claim_token = :claim_token,
                        last_error_code = NULL,
                        updated_at = now()
                    WHERE delivery_id = :delivery_id
                    RETURNING attempt_count
                """),
                {
                    "delivery_id": str(delivery_id),
                    "lease_seconds": self.LEASE_SECONDS,
                    "claim_token": str(claim_token),
                },
            )
            return WebhookDeliveryResult(
                delivery_id, "claimed", updated.scalar_one(), claim_token
            )

    async def mark_processed(self, delivery_id: UUID, *, claim_token: UUID) -> None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("""
                    UPDATE github_webhook_deliveries
                    SET processing_status = 'processed',
                        processed_at = now(),
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error_code = NULL,
                        updated_at = now()
                    WHERE delivery_id = :delivery_id
                      AND processing_status = 'processing'
                      AND claim_token = :claim_token
                """),
                {"delivery_id": str(delivery_id), "claim_token": str(claim_token)},
            )
            if result.rowcount != 1:
                raise RuntimeError("Webhook delivery claim is stale or no longer active.")

    async def mark_failed(
        self, delivery_id: UUID, *, claim_token: UUID, error_code: str
    ) -> None:
        if not error_code or len(error_code) > 100:
            raise ValueError("A bounded, non-empty error code is required.")
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("""
                    UPDATE github_webhook_deliveries
                    SET processing_status = 'failed',
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        last_error_code = :error_code,
                        updated_at = now()
                    WHERE delivery_id = :delivery_id
                      AND processing_status = 'processing'
                      AND claim_token = :claim_token
                """),
                {
                    "delivery_id": str(delivery_id),
                    "claim_token": str(claim_token),
                    "error_code": error_code,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("Webhook delivery claim is stale or no longer active.")
