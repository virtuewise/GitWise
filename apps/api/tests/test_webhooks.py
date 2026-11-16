import hashlib
import hmac

import pytest
from gitwise_api.webhooks import verify_github_webhook_signature


def test_verifies_github_sha256_signature_for_raw_payload() -> None:
    secret = "a-runtime-secret"
    payload = b'{"action":"created","installation":{"id":123}}'
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    assert verify_github_webhook_signature(payload, f"sha256={digest}", secret)


def test_rejects_signature_for_modified_payload() -> None:
    secret = "a-runtime-secret"
    payload = b'{"action":"created"}'
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    assert not verify_github_webhook_signature(
        payload + b" ", f"sha256={digest}", secret
    )


@pytest.mark.parametrize(
    "signature",
    [None, "", "sha1=" + "0" * 40, "sha256=short", "sha256=" + "G" * 64],
)
def test_rejects_malformed_or_missing_signature(signature: str | None) -> None:
    assert not verify_github_webhook_signature(b"{}", signature, "secret")


def test_requires_configured_secret() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        verify_github_webhook_signature(b"{}", "sha256=" + "0" * 64, "")


def test_requires_raw_bytes_payload() -> None:
    with pytest.raises(TypeError, match="raw request bytes"):
        verify_github_webhook_signature("{}".strip(), "sha256=" + "0" * 64, "secret")
