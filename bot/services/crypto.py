"""Fernet encryption for refresh tokens (SPEC 1.5).

Protects a leaked database file or a stray SELECT *, not a compromised host:
the key sits in .env next to data/bot.db. Losing FERNET_KEY means every user
has to connect again — there is no recovery path by design.
"""

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    """Encrypts and decrypts refresh tokens.

    Never put a plaintext token — or this object's input — into a log record
    or an exception message. Errors here carry no payload on purpose.
    """

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            # The key itself must not reach the message: it is a secret too.
            raise ValueError("FERNET_KEY is not a valid Fernet key") from exc

    def encrypt(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode())

    def decrypt(self, blob: bytes) -> str:
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken as exc:
            # Wrong key or a corrupted row. Raised without the ciphertext so a
            # traceback cannot leak it.
            raise ValueError("stored token cannot be decrypted") from exc
