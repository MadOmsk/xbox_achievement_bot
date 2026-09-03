"""/online: presence rendering and the group hub keyboard (SPEC 6.3)."""

from __future__ import annotations

from bot.db.repo import ChatPresenceRow, Repo
from bot.handlers.chat import _presence_icon, _presence_text, hub_keyboard

XUID_A = "xuid-a"
XUID_B = "xuid-b"
CHAT_ID = -100500


def presence(state: str | None, title_id: str | None = None, title_name: str | None = None):
    return ChatPresenceRow(
        tg_id=1, gamertag="Igor", xuid=XUID_A, state=state, title_id=title_id, title_name=title_name
    )


def test_playing_shows_the_game() -> None:
    row = presence("Online", "123", "Halo Infinite")
    assert _presence_text(row) == "играет — Halo Infinite"
    assert _presence_icon(row) == "🟢"


def test_online_not_playing() -> None:
    row = presence("Online", None)
    assert _presence_text(row) == "в сети, не играет"
    assert _presence_icon(row) == "🟡"


def test_offline() -> None:
    row = presence("Offline")
    assert _presence_text(row) == "не в сети"
    assert _presence_icon(row) == "⚪"


def test_never_polled() -> None:
    row = presence(None)
    assert _presence_text(row) == "нет данных"


def test_hub_keyboard_has_exactly_three_buttons_and_carries_the_chat_id() -> None:
    markup = hub_keyboard("mybot", CHAT_ID)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 3
    connect_button = next(b for b in buttons if b.text == "🔗 Подключить Xbox")
    assert connect_button.url is not None
    assert f"start=connect{CHAT_ID}" in connect_button.url


async def test_chat_member_presence_orders_playing_first(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    for tg_id, xuid, tag in ((1, XUID_A, "Offline"), (2, XUID_B, "Playing")):
        await repo.ensure_user(tg_id, tag.lower())
        await repo.link_xbox_account(tg_id, xuid, tag, 0)
        await repo.subscribe(CHAT_ID, tg_id)
    await repo.save_presence_state(XUID_A, "Offline", None, None, changed=True)
    await repo.save_presence_state(XUID_B, "Online", "123", "Halo Infinite", changed=True)

    rows = await repo.chat_member_presence(CHAT_ID)

    assert [row.gamertag for row in rows] == ["Playing", "Offline"]


async def test_chat_exists(repo: Repo) -> None:
    assert await repo.chat_exists(CHAT_ID) is False
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    assert await repo.chat_exists(CHAT_ID) is True
