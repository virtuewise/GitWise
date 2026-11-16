import pytest
from cryptography.fernet import Fernet
from gitwise_api.token_crypto import TokenCipher, TokenCryptoError

TOKEN = "ghu_example_user_access_token"


def test_round_trip_does_not_expose_plaintext() -> None:
    cipher = TokenCipher([Fernet.generate_key()])
    ciphertext = cipher.encrypt(TOKEN)
    assert TOKEN.encode() not in ciphertext
    assert cipher.decrypt(ciphertext) == TOKEN


def test_rotation_keeps_old_ciphertext_readable_until_reencrypted() -> None:
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    old_ciphertext = TokenCipher([old_key]).encrypt(TOKEN)

    rotating = TokenCipher([new_key, old_key])
    assert rotating.decrypt(old_ciphertext) == TOKEN
    reencrypted = rotating.rotate(old_ciphertext)

    new_only = TokenCipher([new_key])
    assert new_only.decrypt(reencrypted) == TOKEN
    with pytest.raises(TokenCryptoError):
        new_only.decrypt(old_ciphertext)


def test_wrong_key_and_tampering_fail_without_leaking_material() -> None:
    ciphertext = TokenCipher([Fernet.generate_key()]).encrypt(TOKEN)
    other = TokenCipher([Fernet.generate_key()])
    with pytest.raises(TokenCryptoError) as wrong_key:
        other.decrypt(ciphertext)
    assert TOKEN not in str(wrong_key.value)
    with pytest.raises(TokenCryptoError):
        other.decrypt(b"not-a-token")


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(TokenCryptoError):
        TokenCipher([])
    with pytest.raises(TokenCryptoError) as invalid:
        TokenCipher(["not-a-valid-fernet-key"])
    assert "not-a-valid-fernet-key" not in str(invalid.value)
    with pytest.raises(TokenCryptoError):
        TokenCipher.from_setting(None)


def test_from_setting_reads_comma_separated_keys_newest_first() -> None:
    new_key, old_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    cipher = TokenCipher.from_setting(f" {new_key} , {old_key} ")
    old_ciphertext = TokenCipher([old_key]).encrypt(TOKEN)
    assert cipher.decrypt(old_ciphertext) == TOKEN
    assert TokenCipher([new_key]).decrypt(cipher.encrypt(TOKEN)) == TOKEN


def test_rejects_empty_plaintext() -> None:
    cipher = TokenCipher([Fernet.generate_key()])
    with pytest.raises(TokenCryptoError):
        cipher.encrypt("")
