"""Tokens are encrypted at rest and never appear in errors (CLAUDE.md)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from bot.services.crypto import TokenCipher
from bot.util import mask

SECRET = "M.C123_BAY.0.U.-Cn!veryLongRefreshTokenValue"


def test_roundtrip(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(SECRET)
    assert blob != SECRET.encode()
    assert SECRET.encode() not in blob
    assert cipher.decrypt(blob) == SECRET


def test_wrong_key_fails_without_leaking(cipher: TokenCipher) -> None:
    blob = cipher.encrypt(SECRET)
    other = TokenCipher(Fernet.generate_key().decode())

    with pytest.raises(ValueError) as info:
        other.decrypt(blob)

    message = str(info.value) + repr(info.value)
    assert SECRET not in message
    assert blob.decode() not in message


def test_bad_key_is_not_echoed() -> None:
    with pytest.raises(ValueError) as info:
        TokenCipher("not-a-fernet-key")
    assert "not-a-fernet-key" not in str(info.value)


def test_mask_reveals_nothing_but_length() -> None:
    masked = mask(SECRET)
    assert str(len(SECRET)) in masked
    for chunk_start in range(len(SECRET) - 3):
        assert SECRET[chunk_start : chunk_start + 4] not in masked
