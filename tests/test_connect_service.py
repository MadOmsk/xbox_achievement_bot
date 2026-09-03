"""ConnectService: state handling and the origin-chat thread-through (SPEC 6.3)."""

from __future__ import annotations

from bot.db.repo import Repo
from bot.services.connect import ConnectError, ConnectService
from bot.services.xbox.auth import XboxIdentity

TG_ID = 42


class FakeAuth:
    def __init__(self) -> None:
        self.stored: list[tuple[int, XboxIdentity]] = []

    def authorization_url(self, state: str) -> str:
        return f"https://login.example/{state}"

    async def exchange_code(self, code: str) -> XboxIdentity:
        return XboxIdentity(xuid="xuid-1", gamertag="Igor", refresh_token="rt")

    async def store_identity(self, tg_id: int, identity: XboxIdentity) -> None:
        self.stored.append((tg_id, identity))


async def test_origin_chat_id_survives_the_round_trip(repo: Repo) -> None:
    """The chat a person pressed «Подключить Xbox» from must come back out of
    complete_login so the caller can auto-subscribe him there (SPEC 6.3)."""
    service = ConnectService(FakeAuth(), repo)  # type: ignore[arg-type]
    url = service.start_login(TG_ID, origin_chat_id=-1001234567890)
    state = url.rsplit("/", 1)[1]

    tg_id, identity, origin_chat_id = await service.complete_login(state, "code")

    assert tg_id == TG_ID
    assert identity.gamertag == "Igor"
    assert origin_chat_id == -1001234567890


async def test_no_origin_chat_when_started_without_one(repo: Repo) -> None:
    service = ConnectService(FakeAuth(), repo)  # type: ignore[arg-type]
    url = service.start_login(TG_ID)
    state = url.rsplit("/", 1)[1]

    _, _, origin_chat_id = await service.complete_login(state, "code")

    assert origin_chat_id is None


async def test_unknown_state_is_a_connect_error(repo: Repo) -> None:
    service = ConnectService(FakeAuth(), repo)  # type: ignore[arg-type]
    try:
        await service.complete_login("nonsense", "code")
    except ConnectError:
        pass
    else:
        raise AssertionError("expected ConnectError")
