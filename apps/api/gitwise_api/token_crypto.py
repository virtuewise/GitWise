"""Encrypt GitHub user tokens at rest with support for key rotation."""

from __future__ import annotations

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class TokenCryptoError(RuntimeError):
    """A token could not be encrypted or decrypted. Never carries token material."""


class TokenCipher:
    """Authenticated encryption for stored OAuth tokens.

    Keys are listed newest first. New ciphertext always uses the first key; any
    listed key can still decrypt, so a key can be rotated in without downtime and
    retired once ``rotate`` has re-encrypted stored values.
    """

    def __init__(self, keys: Sequence[str | bytes]) -> None:
        if not keys:
            raise TokenCryptoError("At least one token encryption key is required.")
        try:
            self._fernet = MultiFernet([Fernet(key) for key in keys])
        except (ValueError, TypeError):
            raise TokenCryptoError("Token encryption keys must be valid Fernet keys.") from None

    @classmethod
    def from_setting(cls, value: str | None) -> TokenCipher:
        """Build a cipher from a comma-separated key list, newest key first."""
        keys = [part.strip() for part in (value or "").split(",") if part.strip()]
        return cls(keys)

    def encrypt(self, plaintext: str) -> bytes:
        if not isinstance(plaintext, str) or not plaintext:
            raise TokenCryptoError("A non-empty token is required.")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(ciphertext)).decode("utf-8")
        except (InvalidToken, TypeError, ValueError):
            raise TokenCryptoError("Stored token could not be decrypted.") from None

    def rotate(self, ciphertext: bytes) -> bytes:
        """Re-encrypt a stored value under the newest key."""
        try:
            return self._fernet.rotate(bytes(ciphertext))
        except (InvalidToken, TypeError, ValueError):
            raise TokenCryptoError("Stored token could not be rotated.") from None
