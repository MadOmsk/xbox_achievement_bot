"""The headline gamerscore next to a name must always be the profile value,
never a sum over `seen_achievements` (SPEC 5.4) — the sum is permanently
best-effort (title_history is capped, achievements with no unlock date exist,
etc.), while the profile number is what a person actually sees on the Xbox
site. Locked down here after chasing the opposite assumption for a while."""

from __future__ import annotations

from bot.db.repo import AchievementRow, Repo
from bot.handlers.chat import _build_stats_text
from bot.util import utcnow

XUID = "xuid-profile-check"


def _achievement(title_id: str) -> AchievementRow:
    return AchievementRow(
        title_id=title_id,
        achievement_id="1",
        name="A",
        description=None,
        icon_url=None,
        unlocked_at=utcnow().isoformat(timespec="seconds"),
        gamerscore=10,
        rarity_percent=50.0,
        platform="modern",
    )


async def test_header_gamerscore_is_the_profile_value_not_a_sum(repo: Repo) -> None:
    await repo.ensure_user(1, "someone")
    # Profile says a lot more than seen_achievements will ever sum to.
    await repo.link_xbox_account(1, XUID, "Someone", 999_999)
    await repo.insert_new_achievements(
        XUID,
        [
            AchievementRow(
                title_id="1",
                achievement_id="1",
                name="An achievement",
                description=None,
                icon_url=None,
                unlocked_at="2026-01-01T00:00:00+00:00",
                gamerscore=10,
                rarity_percent=50.0,
                platform="modern",
            )
        ],
        is_backfill=False,
    )

    user = await repo.get_user(1)
    assert user is not None
    built = await _build_stats_text(repo, user)

    assert built is not None
    text, _markup = built
    assert "999" in text.split("\n")[0]  # thousands() formatting, profile value
    assert "10 G" not in text.split("\n")[0]


async def test_stats_offers_show_all_games_button_only_past_the_limit(repo: Repo) -> None:
    """stats_games_limit (default 15) caps the "Игры за 30 дней" table; past
    it a "Показать все игры" button should appear (SPEC 6.3)."""
    await repo.ensure_user(1, "someone")
    await repo.link_xbox_account(1, XUID, "Someone", 0)
    await repo.set_app_setting("stats_games_limit", "2")
    for i in range(3):
        await repo.insert_new_achievements(XUID, [_achievement(str(i))], is_backfill=False)

    user = await repo.get_user(1)
    assert user is not None
    built = await _build_stats_text(repo, user)

    assert built is not None
    text, markup = built
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.callback_data == f"stats:allgames:{XUID}" for b in buttons)
    # 3 distinct games exist, but only 2 (the limit) render as table rows.
    assert text.count("без названия") == 2


async def test_stats_has_no_show_all_games_button_under_the_limit(repo: Repo) -> None:
    await repo.ensure_user(1, "someone")
    await repo.link_xbox_account(1, XUID, "Someone", 0)
    await repo.insert_new_achievements(XUID, [_achievement("1")], is_backfill=False)

    user = await repo.get_user(1)
    assert user is not None
    built = await _build_stats_text(repo, user)

    assert built is not None
    _text, markup = built
    assert markup is None
