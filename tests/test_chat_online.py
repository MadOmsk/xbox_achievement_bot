"""/online: presence rendering and the group hub keyboard (SPEC 6.3)."""

from __future__ import annotations

from bot.db.repo import ChatPresenceRow, Repo
from bot.handlers.chat import HELP_TEXT, _presence_icon, _presence_text, hub_keyboard

XUID_A = "xuid-a"
XUID_B = "xuid-b"
CHAT_ID = -100500


def presence(
    state: str | None,
    title_id: str | None = None,
    title_name: str | None = None,
    platform: str = "modern",
):
    return ChatPresenceRow(
        tg_id=1,
        gamertag="Igor",
        xuid=XUID_A,
        state=state,
        title_id=title_id,
        title_name=title_name,
        platform=platform,
    )


def test_playing_shows_the_game() -> None:
    row = presence("Online", "123", "Halo Infinite")
    assert _presence_text(row) == "играет — Halo Infinite"


def test_online_not_playing() -> None:
    row = presence("Online", None)
    assert _presence_text(row) == "в сети, не играет"


def test_offline() -> None:
    row = presence("Offline")
    assert _presence_text(row) == "не в сети"


def test_never_polled() -> None:
    row = presence(None)
    assert _presence_text(row) == "нет данных"


def test_presence_icon_is_platform_colour_while_online() -> None:
    """SPEC 9, M-Steam-2e: online (playing or not) — the circle marks which
    platform, not whether they're playing (status is already in the text
    next to it). Playing and merely-online both get the platform colour."""
    assert _presence_icon(presence("Online", "123", "Halo Infinite", platform="modern")) == "🟢"
    assert _presence_icon(presence("Online", None, platform="modern")) == "🟢"
    assert _presence_icon(presence("Online", "550", "L4D2", platform="steam")) == "⚫"
    assert _presence_icon(presence("Online", None, platform="steam")) == "⚫"


def test_presence_icon_is_grey_when_offline_regardless_of_platform() -> None:
    """Found live: a pure platform colour made offline rows indistinguishable
    from online ones — grey is the one thing a status colour is actually
    good for, and it should mean the same thing on every platform."""
    assert _presence_icon(presence("Offline", platform="modern")) == "⚪"
    assert _presence_icon(presence("Offline", platform="steam")) == "⚪"
    assert _presence_icon(presence(None, platform="modern")) == "⚪"
    assert _presence_icon(presence(None, platform="steam")) == "⚪"


def test_hub_keyboard_has_exactly_four_buttons_and_carries_the_chat_id() -> None:
    markup = hub_keyboard("mybot", CHAT_ID)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 4
    connect_button = next(b for b in buttons if b.text == "🔗 Подключить XBOX")
    assert connect_button.url is not None
    assert f"start=connect{CHAT_ID}" in connect_button.url
    steam_button = next(b for b in buttons if b.text == "🎮 Подключить Steam")
    assert steam_button.url == "https://t.me/mybot?start=connectsteam"


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
    assert {row.platform for row in rows} == {"modern"}


async def test_chat_member_presence_reports_platform_for_steam_only_and_mixed(
    repo: Repo,
) -> None:
    """SPEC 9, M-Steam-2e: platform is exposed so /online can colour the
    icon — a Steam-only person defaults to 'steam' with no presence data
    at all, and whichever platform actually updated more recently wins for
    someone with both connected."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)

    await repo.ensure_user(1, "steamonly")
    await repo.link_platform_account(1, "steam", "76561197960287930", "SteamOnly")
    await repo.subscribe(CHAT_ID, 1)

    await repo.ensure_user(2, "both")
    await repo.link_xbox_account(2, XUID_B, "Both", 0)
    await repo.link_platform_account(2, "steam", "76561197981065056", "BothSteam")
    await repo.subscribe(CHAT_ID, 2)
    await repo.save_presence_state(XUID_B, "Online", "123", "Halo Infinite", changed=True)
    await repo.save_steam_presence_state("76561197981065056", 1, "550", "L4D2", changed=True)
    # Both writes land in the same second at second-resolution timestamps —
    # backdate the Xbox one so which is "fresher" is unambiguous, the same
    # as it always would be at real 60s-apart tick granularity.
    await repo._conn.execute(
        "UPDATE presence_state SET updated_at = '2020-01-01T00:00:00+00:00' WHERE xuid = ?",
        (XUID_B,),
    )
    await repo._conn.commit()

    rows = {row.tg_id: row for row in await repo.chat_member_presence(CHAT_ID)}

    assert rows[1].platform == "steam"  # no presence data at all, only a Steam link
    assert rows[2].platform == "steam"  # both playing (tied activity level), Steam fresher


async def test_playing_on_one_platform_beats_merely_online_on_the_other(repo: Repo) -> None:
    """Found live: Mad Omsk was playing on Steam and merely online (not
    playing) on Xbox, but /online showed Xbox — because the first version
    of this merge compared `updated_at` alone, and Xbox happened to get
    polled a moment later (every poll bumps updated_at, changed or not).
    'Playing' must always outrank 'online' regardless of which platform
    updated more recently — freshness only breaks a tie at the *same*
    activity level (SPEC 9, M-Steam-2e, "играет > онлайн > офлайн")."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    await repo.ensure_user(1, "madomsk")
    await repo.link_xbox_account(1, XUID_A, "MadOmsk", 0)
    await repo.link_platform_account(1, "steam", "76561197981065056", "MadOmskSteam")
    await repo.subscribe(CHAT_ID, 1)

    # Steam: playing. Xbox: online, not playing — but polled after Steam,
    # so its updated_at is the more recent one.
    await repo.save_steam_presence_state(
        "76561197981065056", 1, "550", "Left 4 Dead 2", changed=True
    )
    await repo.save_presence_state(XUID_A, "Online", None, None, changed=True)

    rows = await repo.chat_member_presence(CHAT_ID)

    assert len(rows) == 1
    row = rows[0]
    assert row.platform == "steam"
    assert row.title_id == "550"
    assert row.title_name == "Left 4 Dead 2"


async def test_chat_exists(repo: Repo) -> None:
    assert await repo.chat_exists(CHAT_ID) is False
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    assert await repo.chat_exists(CHAT_ID) is True


async def test_online_lists_a_connected_non_publisher_who_was_seen_writing(
    repo: Repo,
) -> None:
    """/online must not be just the publisher list — someone connected who
    never pressed "Публиковать" but did write here should still show up
    (SPEC 6.3, the "test chat" bug: 2 people in the chat, /online showed 1)."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    await repo.ensure_user(1, "publisher")
    await repo.link_xbox_account(1, XUID_A, "Publisher", 0)
    await repo.subscribe(CHAT_ID, 1)

    await repo.ensure_user(2, "lurker")
    await repo.link_xbox_account(2, XUID_B, "Lurker", 0)
    await repo.record_chat_seen(CHAT_ID, 2)  # wrote here, never subscribed

    rows = await repo.chat_member_presence(CHAT_ID)

    assert {row.gamertag for row in rows} == {"Publisher", "Lurker"}


def test_help_text_mentions_both_platforms_and_the_main_commands() -> None:
    """Rewritten 2026-09-05: no more connect/subscribe walkthrough in the
    text — the hub's own buttons (hub_keyboard) already cover both,
    intuitively enough on their own — just what the bot is and the
    commands people actually come back to use."""
    intro = HELP_TEXT.split("\n\n")[0].lower()
    assert "xbox" in intro and "steam" in intro
    for command in ("/stats", "/online", "/who", "/recent", "/summary", "/hltb"):
        assert command in HELP_TEXT
    assert "/subscribe" not in HELP_TEXT
    assert "/unsubscribe" not in HELP_TEXT


async def test_record_chat_seen_ignores_an_unknown_tg_id(repo: Repo) -> None:
    """Same rule as update_username: writing in a chat must not create a user
    row for someone the bot has never otherwise seen."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    await repo.record_chat_seen(CHAT_ID, 999999)  # no such user — must not raise

    rows = await repo.chat_member_presence(CHAT_ID)

    assert rows == []
