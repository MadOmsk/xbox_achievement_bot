"""Microsoft sign-in and token lifecycle (SPEC 5.1, 5.1.2).

The library owns the OAuth dance; this module owns *when* tokens are refreshed
and *where* they are stored. Both rules that break silently live here:

  1. a new refresh token is written to the database BEFORE the request that
     uses it — Microsoft invalidates the old one the moment it issues a new one,
     so a crash in between would sign the person out for no reason;
  2. one lock per user around the whole procedure — two concurrent refreshes of
     the same token reliably destroy it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import httpx
from xbox.webapi.authentication.manager import AuthenticationManager
from xbox.webapi.authentication.models import OAuth2TokenResponse
from xbox.webapi.common.signed_session import SignedSession

from bot.config import Settings
from bot.db.repo import Repo
from bot.services.crypto import TokenCipher
from bot.util import utcnow

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3


class NotConnectedError(Exception):
    """The user has never linked an Xbox account."""


class TokenRefreshError(Exception):
    """Refresh failed, but the token may still be alive (network, 5xx)."""


class TokenDeadError(TokenRefreshError):
    """Microsoft refused the refresh token: consent revoked, password changed,
    token expired or account blocked. Indistinguishable from each other and
    cured the same way — a new /connect_xbox (SPEC 5.1.2)."""


@dataclass(slots=True)
class XboxIdentity:
    xuid: str
    gamertag: str
    refresh_token: str


class XboxAuthService:
    def __init__(self, settings: Settings, repo: Repo, cipher: TokenCipher) -> None:
        self._settings = settings
        self._repo = repo
        self._cipher = cipher
        # Called when a token is declared dead. A plain callback rather than a
        # Telegram object: this layer must not know the bot exists (CLAUDE.md).
        self.on_token_dead: Callable[[int], Awaitable[None]] | None = None
        self._session: SignedSession | None = None
        self._managers: dict[int, AuthenticationManager] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def start(self) -> None:
        self._session = SignedSession()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    # ------------------------------------------------------------- sign-in

    def authorization_url(self, state: str) -> str:
        """URL of the Microsoft consent screen. `state` carries the tg_id."""
        return self._manager().generate_authorization_url(state)

    async def exchange_code(self, code: str) -> XboxIdentity:
        """Finish the OAuth callback: code -> tokens -> XUID and gamertag.

        The XSTS response already carries both, so no profile request is needed.
        """
        manager = self._manager()
        try:
            await manager.request_tokens(code)
        except httpx.HTTPStatusError as exc:
            log.warning("code exchange rejected: %s", _describe(exc))
            raise TokenDeadError("authorization code rejected") from None

        assert manager.oauth is not None and manager.xsts_token is not None
        refresh_token = manager.oauth.refresh_token
        if not refresh_token:
            # Without offline_access there is nothing to store and the account
            # would silently stop working in an hour.
            raise TokenDeadError("Microsoft returned no refresh token")

        return XboxIdentity(
            xuid=manager.xsts_token.xuid,
            gamertag=manager.xsts_token.gamertag,
            refresh_token=refresh_token,
        )

    async def store_identity(self, tg_id: int, identity: XboxIdentity) -> None:
        await self._repo.save_refresh_token(tg_id, self._cipher.encrypt(identity.refresh_token))
        await self._repo.link_xbox_account(
            tg_id, xuid=identity.xuid, gamertag=identity.gamertag, gamerscore=None
        )
        self._managers.pop(tg_id, None)

    # ------------------------------------------------------------ refresh

    async def authenticated_manager(self, tg_id: int) -> AuthenticationManager:
        """Return a manager whose XSTS token is good for the next few minutes.

        Lazy: called right before a request, never on a schedule.
        """
        async with self._locks.setdefault(tg_id, asyncio.Lock()):
            manager = self._managers.get(tg_id) or await self._restore_manager(tg_id)

            try:
                if not self._fresh(manager.oauth):
                    oauth = await manager.refresh_oauth_token()
                    manager.oauth = oauth
                    # Before anything else touches the network (SPEC 5.1).
                    await self._persist_refresh_token(tg_id, oauth)
                    manager.user_token = None
                    manager.xsts_token = None

                if not self._fresh(manager.user_token):
                    manager.user_token = await manager.request_user_token()
                if not self._fresh(manager.xsts_token):
                    manager.xsts_token = await manager.request_xsts_token()
            except httpx.HTTPStatusError as exc:
                secret = manager.oauth.refresh_token if manager.oauth else None
                await self._on_http_error(tg_id, exc, secret)
                raise  # unreachable: _on_http_error always raises
            except httpx.RequestError as exc:
                # A timeout is not a dead token.
                raise await self._on_network_error(tg_id, exc) from None

            self._managers[tg_id] = manager
            return manager

    async def _restore_manager(self, tg_id: int) -> AuthenticationManager:
        record = await self._repo.get_token(tg_id)
        if record is None:
            raise NotConnectedError(f"user {tg_id} has no token")
        if record.status != "active":
            raise TokenDeadError(f"token of user {tg_id} is {record.status}")

        manager = self._manager()
        # A placeholder OAuth response that only carries the refresh token:
        # expires_in=0 makes the very next check refresh it.
        manager.oauth = OAuth2TokenResponse(
            token_type="Bearer",
            expires_in=0,
            scope="",
            access_token="",
            user_id="",
            refresh_token=self._cipher.decrypt(record.refresh_token_enc),
        )
        return manager

    async def _persist_refresh_token(self, tg_id: int, oauth: OAuth2TokenResponse) -> None:
        if not oauth.refresh_token:
            raise TokenDeadError("refresh response carried no new refresh token")
        await self._repo.save_refresh_token(tg_id, self._cipher.encrypt(oauth.refresh_token))

    async def _on_http_error(
        self, tg_id: int, exc: httpx.HTTPStatusError, secret: str | None = None
    ) -> None:
        detail = _scrub(_describe(exc), secret)
        if _is_invalid_grant(exc):
            # Log everything Microsoft said; the user only sees "access expired"
            # because none of these variants changes what he has to do.
            log.warning("token of tg_id=%s refused by Microsoft: %s", tg_id, detail)
            await self._kill(tg_id)
            raise TokenDeadError("refresh token rejected") from None
        log.warning("refresh for tg_id=%s failed: %s", tg_id, detail)
        raise await self._count_failure(tg_id, detail) from None

    async def _on_network_error(self, tg_id: int, exc: httpx.RequestError) -> Exception:
        return await self._count_failure(tg_id, f"{type(exc).__name__}: {exc}")

    async def _count_failure(self, tg_id: int, detail: str) -> Exception:
        failures = await self._repo.bump_token_failure(tg_id)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            log.warning("token of tg_id=%s failed %s times in a row: %s", tg_id, failures, detail)
            await self._kill(tg_id)
            return TokenDeadError("refresh failed repeatedly")
        return TokenRefreshError(detail)

    async def _kill(self, tg_id: int) -> None:
        await self._repo.set_token_status(tg_id, "invalid")
        self._managers.pop(tg_id, None)
        if self.on_token_dead is not None:
            try:
                await self.on_token_dead(tg_id)
            except Exception:
                log.exception("token-dead callback failed for tg_id=%s", tg_id)

    # ------------------------------------------------------------ helpers

    def _manager(self) -> AuthenticationManager:
        if self._session is None:
            raise RuntimeError("XboxAuthService.start() was not awaited")
        return AuthenticationManager(
            self._session,
            self._settings.azure_client_id,
            self._settings.azure_client_secret.get_secret_value(),
            self._settings.oauth_redirect_url,
        )

    def _fresh(self, token: object) -> bool:
        """Valid for at least token_refresh_margin more seconds.

        Not "not expired yet": the response has to travel back, and a token that
        dies on the way looks exactly like a broken login.
        """
        if token is None:
            return False
        margin = timedelta(seconds=self._settings.token_refresh_margin)
        deadline = utcnow() + margin
        if isinstance(token, OAuth2TokenResponse):
            return token.issued + timedelta(seconds=token.expires_in) > deadline
        not_after = getattr(token, "not_after", None)
        return bool(not_after and not_after > deadline)


def _is_invalid_grant(exc: httpx.HTTPStatusError) -> bool:
    return _error_body(exc).get("error") == "invalid_grant"


def _describe(exc: httpx.HTTPStatusError) -> str:
    """Everything Microsoft told us, for the log only (SPEC 5.1.2).

    Carries no token: the request body is not touched, only the error response.
    """
    body = _error_body(exc)
    fields = {
        "http": exc.response.status_code,
        "error": body.get("error"),
        "suberror": body.get("suberror"),
        "description": body.get("error_description"),
    }
    return ", ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


def _scrub(text: str, secret: str | None) -> str:
    """Last line of defence before a log call.

    Microsoft has no reason to echo a refresh token back at us, but the rule is
    "never in the log", not "probably not in the log".
    """
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


def _error_body(exc: httpx.HTTPStatusError) -> dict[str, object]:
    try:
        body = exc.response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
