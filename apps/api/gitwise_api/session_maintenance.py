"""Run bounded OAuth session-retention maintenance.

Invoke daily from the deployment's scheduler:
    python -m gitwise_api.session_maintenance

The command prints only aggregate deletion counts, never session identifiers or
credential material. Each batch is its own transaction so maintenance is bounded.
"""

from __future__ import annotations

import asyncio
import logging

from gitwise_api.config import get_settings
from gitwise_api.sessions import SessionStore
from gitwise_api.token_crypto import TokenCipher

logger = logging.getLogger("gitwise.session_maintenance")
BATCH_SIZE = 500


async def _purge() -> int:
    settings = get_settings()
    if not settings.database_url or not settings.token_encryption_keys:
        raise RuntimeError(
            "GITWISE_DATABASE_URL and GITWISE_TOKEN_ENCRYPTION_KEYS are required."
        )
    cipher = TokenCipher.from_setting(settings.token_encryption_keys.get_secret_value())
    store = SessionStore.from_url(settings.database_url, cipher)
    try:
        total = 0
        while True:
            deleted = await store.purge_terminal_sessions(batch_size=BATCH_SIZE)
            total += deleted
            if deleted < BATCH_SIZE:
                return total
    finally:
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        deleted = asyncio.run(_purge())
    except Exception:
        logger.exception("Session retention maintenance failed.")
        raise SystemExit(1) from None
    logger.info("Session retention maintenance completed; deleted_rows=%d", deleted)


if __name__ == "__main__":
    main()
