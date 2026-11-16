"""GitHub webhook request authentication primitives."""

from __future__ import annotations

import hashlib
import hmac
import re

_SIGNATURE_PATTERN = re.compile(r"^sha256=[0-9a-f]{64}$")


def verify_github_webhook_signature(
    payload: bytes,
    signature_header: str | None,
    secret: str | bytes,
) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header against the raw request body.

    Call this before parsing JSON or trusting delivery/event headers. The caller
    must obtain the secret from runtime secret storage, never from request data.
    """
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not secret_bytes:
        raise ValueError("A non-empty GitHub webhook secret must be configured.")
    if not isinstance(payload, bytes):
        raise TypeError("Webhook payload must be the original raw request bytes.")
    if not isinstance(signature_header, str) or not _SIGNATURE_PATTERN.fullmatch(
        signature_header
    ):
        return False

    expected = "sha256=" + hmac.new(
        secret_bytes, payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
