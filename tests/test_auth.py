"""Token refresh — the part of the project that breaks silently (SPEC 5.1)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
from xbox.webapi.authentication.models import OAuth2TokenResponse

from bot.config import Settings
from bot.db.repo import Repo
from bot.services.crypto import TokenCipher
from bot.services.xbox.auth import (
    TokenDeadError,
    TokenRefreshError,
    XboxAuthService,
)
from bot.util import utcnow

OLD_TOKEN = "old-refresh-token-value"
NEW_TOKEN = "new-refresh-token-value"
TG_ID = 42


class FakeXToken:
    def __init__(self) -> None:
        self.not_after = utcnow() + timedelta(hours=1)


class FakeManager:
    """Stands in for AuthenticationManager: no network, but the same call order."""

    def __init__(self, on_call: list[str], refresh_error: Exception | None = None) -> None:
        self.oauth: OAuth2TokenResponse | None = None
        self.user_token: FakeXToken | None = None
        self.xsts_token: FakeXToken | None = None
        self._calls = on_call
        self._refresh_error = refresh_error

    async def refresh_oauth_token(self) -> OAuth2TokenResponse:
        self._calls.append("refresh")
        if self._refresh_error is not None:
            raise self._refresh_error
        return OAuth2TokenResponse(
            token_type="Bearer",
            expires_in=3600,
            scope="XboxLive.signin",
            access_token="access",
            user_id="uid",
            refresh_token=NEW_TOKEN,
        )

    async def request_user_token(self) -> FakeXToken:
        self._calls.append("user_token")
        return FakeXToken()

    async def request_xsts_token(self) -> FakeXToken:
        self._calls.append("xsts")
        return FakeXToken()


def http_error(status: int, body: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://login.live.com/oauth20_token.srf")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


async def _connected_user(repo: Repo, cipher: TokenCipher) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await repo.save_refresh_token(TG_ID, cipher.encrypt(OLD_TOKEN))


def _service(
    settings: Settings,
    repo: Repo,
    cipher: TokenCipher,
    manager: FakeManager,
) -> XboxAuthService:
    service = XboxAuthService(settings, repo, cipher)
    service._manager = lambda: manager  # type: ignore[assignment,method-assign]
    return service


async def test_new_token_is_stored_before_any_further_request(
    settings: Settings, repo: Repo, cipher: TokenCipher
) -> None:
    """SPEC 5.1: Microsoft kills the old token the moment it issues a new one,
    so the new one must be in the database before the next request goes out."""
    calls: list[str] = []
    seen_at_user_token: list[str] = []
    manager = FakeManager(calls)

    original = manager.request_user_token

    async def spy() -> FakeXToken:
        record = await repo.get_token(TG_ID)
        assert record is not None
        seen_at_user_token.append(cipher.decrypt(record.refresh_token_enc))
        return await original()

    manager.request_user_token = spy  # type: ignore[method-assign]

    await _connected_user(repo, cipher)
    await _service(settings, repo, cipher, manager).authenticated_manager(TG_ID)

    assert calls == ["refresh", "user_token", "xsts"]
    assert seen_at_user_token == [NEW_TOKEN]


async def test_parallel_refreshes_are_serialised(
    settings: Settings, repo: Repo, cipher: TokenCipher
) -> None:
    """Two concurrent refreshes of one token destroy it — the per-user lock
    must turn them into one refresh."""
    calls: list[str] = []
    manager = FakeManager(calls)
    await _connected_user(repo, cipher)
    service = _service(settings, repo, cipher, manager)

    await asyncio.gather(
        service.authenticated_manager(TG_ID),
        service.authenticated_manager(TG_ID),
        service.authenticated_manager(TG_ID),
    )

    assert calls.count("refresh") == 1


async def test_invalid_grant_kills_the_token(
    settings: Settings, repo: Repo, cipher: TokenCipher
) -> None:
    manager = FakeManager([], refresh_error=http_error(400, {"error": "invalid_grant"}))
    await _connected_user(repo, cipher)
    service = _service(settings, repo, cipher, manager)

    with pytest.raises(TokenDeadError):
        await service.authenticated_manager(TG_ID)

    record = await repo.get_token(TG_ID)
    assert record is not None
    assert record.status == "invalid"
    assert record.invalid_at is not None


async def test_network_error_is_not_a_dead_token(
    settings: Settings, repo: Repo, cipher: TokenCipher
) -> None:
    error = httpx.ConnectTimeout("timed out")
    manager = FakeManager([], refresh_error=error)
    await _connected_user(repo, cipher)
    service = _service(settings, repo, cipher, manager)

    for _ in range(2):
        with pytest.raises(TokenRefreshError) as info:
            await service.authenticated_manager(TG_ID)
        assert not isinstance(info.value, TokenDeadError)

    record = await repo.get_token(TG_ID)
    assert record is not None
    assert record.status == "active"
    assert record.fail_count == 2

    # Third failure in a row: now we give up (SPEC 5.1).
    with pytest.raises(TokenDeadError):
        await service.authenticated_manager(TG_ID)
    record = await repo.get_token(TG_ID)
    assert record is not None
    assert record.status == "invalid"


async def test_token_never_reaches_logs_or_exceptions(
    settings: Settings,
    repo: Repo,
    cipher: TokenCipher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = FakeManager(
        [],
        refresh_error=http_error(
            400,
            {
                "error": "invalid_grant",
                "suberror": "consent_required",
                "error_description": f"token {OLD_TOKEN} was revoked",
            },
        ),
    )
    await _connected_user(repo, cipher)
    service = _service(settings, repo, cipher, manager)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TokenDeadError) as info:
            await service.authenticated_manager(TG_ID)

    assert OLD_TOKEN not in str(info.value)
    assert OLD_TOKEN not in repr(info.value)
    # Even when Microsoft echoes the token back in its own error text, it is
    # scrubbed before the log call: the rule is "never", not "probably not".
    assert caplog.records, "the refusal must be logged at all"
    for record in caplog.records:
        assert OLD_TOKEN not in record.getMessage()
