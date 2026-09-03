"""The headline gamerscore next to a name must always be the profile value,
never a sum over `seen_achievements` (SPEC 5.4) — the sum is permanently
best-effort (title_history is capped, achievements with no unlock date exist,
etc.), while the profile number is what a person actually sees on the Xbox
site. Locked down here after chasing the opposite assumption for a while."""

from __future__ import annotations

from bot.db.repo import AchievementRow, Repo
from bot.handlers.chat import _build_stats_text

XUID = "xuid-profile-check"


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
    text = await _build_stats_text(repo, user)

    assert text is not None
    assert "999" in text.split("\n")[0]  # thousands() formatting, profile value
    assert "10 G" not in text.split("\n")[0]
