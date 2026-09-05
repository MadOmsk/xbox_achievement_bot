"""/recent as a readable list, not a code-block table (SPEC 6.3)."""

from __future__ import annotations

from bot.db.repo import AchievementRow, RecentAchievement, Repo
from bot.handlers.chat import _recent_list, _recent_row

CHAT_ID = -100777


def row(
    gamertag: str | None = "Igor",
    name: str = "Ashes to Ashes",
    game: str | None = "Halo Infinite",
    gamerscore: int = 50,
    rarity_percent: float | None = 2.4,
    is_secret: bool = False,
) -> RecentAchievement:
    return RecentAchievement(
        gamertag=gamertag,
        name=name,
        game=game,
        gamerscore=gamerscore,
        rarity_percent=rarity_percent,
        platform="modern",
        unlocked_at="2026-09-02T10:00:00+00:00",
        is_secret=is_secret,
    )


def test_recent_row_mentions_rarity_badge() -> None:
    """The badge leads the line now, not a fixed generic bullet (2026-09-05)
    — it always shows one of the two icons, never both a bullet and a badge
    on the same "common" row."""
    assert _recent_row(row(rarity_percent=2.4)).startswith("💎 ")
    assert _recent_row(row(rarity_percent=None)).startswith("🏆 ")


def test_recent_row_gamerscore_and_rarity_are_in_one_parenthetical() -> None:
    assert "(+50 G · 2.4%)" in _recent_row(row(gamerscore=50, rarity_percent=2.4))


def test_recent_row_hides_zero_gamerscore_but_keeps_rarity() -> None:
    """Found live: every Steam row showed a flat "+0 G" — Steam achievements
    have no gamerscore at all, same "0 is 0, don't name it" rule the
    achievement message itself already follows (2026-09-05)."""
    line = _recent_row(row(gamerscore=0, rarity_percent=12.0))
    assert "G" not in line
    assert "(12%)" in line


def test_recent_row_rarity_has_no_label_word() -> None:
    """Unlike the achievement message's own "редкость 12%" — /recent's
    parenthetical is bare percentages, the badge already says rare or not
    (2026-09-05)."""
    line = _recent_row(row(rarity_percent=12.0))
    assert "редкость" not in line
    assert "12%" in line


def test_recent_row_omits_the_parenthetical_entirely_when_nothing_to_show() -> None:
    line = _recent_row(row(gamerscore=0, rarity_percent=None))
    assert "(" not in line


def test_recent_row_shows_the_platform_icon_before_the_game_name() -> None:
    line = _recent_row(row(game="Left 4 Dead 2"))
    steam_line = _recent_row(
        RecentAchievement(
            gamertag="Igor",
            name="Boomer",
            game="Left 4 Dead 2",
            gamerscore=0,
            rarity_percent=None,
            platform="steam",
            unlocked_at="2026-09-02T10:00:00+00:00",
        )
    )
    assert "🟢 Left 4 Dead 2" in line  # default platform="modern" from row()
    assert "⚫ Left 4 Dead 2" in steam_line


def test_recent_row_long_names_are_truncated() -> None:
    long_name = "A" * 40
    line = _recent_row(row(gamertag=long_name, name=long_name, game=long_name))
    assert "A" * 40 not in line
    assert "…" in line


def test_secret_achievement_name_is_a_real_spoiler_not_a_placeholder() -> None:
    """A blockquote (unlike the old <pre> table) can host a real Telegram
    spoiler — the actual name stays in the message, just hidden (SPEC 7.1)."""
    line = _recent_row(row(name="Ashes to Ashes", is_secret=True))
    assert '<span class="tg-spoiler">Ashes to Ashes</span>' in line


def test_non_secret_row_has_no_spoiler_markup() -> None:
    line = _recent_row(row(name="Ashes to Ashes", is_secret=False))
    assert "tg-spoiler" not in line
    assert "Ashes to Ashes" in line


def test_recent_row_text_is_html_escaped() -> None:
    line = _recent_row(row(gamertag="We>ird<Name", name="A&B<C>", game="Hal&o"))
    assert "<C>" not in line
    assert "&amp;" in line and "&lt;" in line


def test_recent_list_is_a_collapsible_blockquote() -> None:
    text = _recent_list([row(), row(gamertag="Alex", rarity_percent=None)])
    assert text.startswith("<blockquote expandable>")
    assert text.endswith("</blockquote>")
    assert "Igor" in text and "Alex" in text


async def test_chat_recent_includes_a_steam_only_person(repo: Repo) -> None:
    """Found live: chat_recent()'s own JOIN was keyed on `xuid`, which is
    Xbox-only on `users` — a Steam-only person (no xuid at all) never
    matched, and even a person with both platforms only ever saw their
    Xbox rows. Fixed to join on tg_id, the column seen_achievements always
    carries regardless of platform (SPEC 9, M-Steam-2a)."""
    await repo.upsert_chat(CHAT_ID, "Чат", 1)
    await repo.ensure_user(1, "steamonly")
    await repo.link_platform_account(1, "steam", "76561197960287930", "SteamOnly")
    await repo.subscribe(CHAT_ID, 1)
    await repo.insert_new_achievements_steam(
        1,
        "76561197960287930",
        [
            AchievementRow(
                title_id="550",
                achievement_id="a1",
                name="Boomer",
                description=None,
                icon_url=None,
                unlocked_at="2026-09-05T00:00:00+00:00",
                gamerscore=0,
                rarity_percent=10.0,
                platform="steam",
                title_name="Left 4 Dead 2",
            )
        ],
        is_backfill=False,
    )

    rows = await repo.chat_recent(CHAT_ID, 10)

    assert len(rows) == 1
    assert rows[0].name == "Boomer"
    # Also covers insert_new_achievements_steam's own titles-cache fix
    # (was never populated for Steam at all, unlike Xbox's ensure_title_name)
    # — without it this would render "без названия" instead of a real name.
    assert rows[0].game == "Left 4 Dead 2"
