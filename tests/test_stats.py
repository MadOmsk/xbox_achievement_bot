"""Counters and day boundaries (SPEC 5.9)."""

from __future__ import annotations

from datetime import UTC, datetime

from bot.db.repo import AchievementRow, Repo
from bot.services.stats import counters_for, day_start_utc, month_start_utc

XUID = "2533274829605736"


def row(achievement_id: str, unlocked_at: str | None, score: int = 10) -> AchievementRow:
    return AchievementRow(
        title_id="1",
        achievement_id=achievement_id,
        name=achievement_id,
        description=None,
        icon_url=None,
        unlocked_at=unlocked_at,
        gamerscore=score,
        rarity_percent=None,
        platform="modern",
    )


def test_day_boundary_follows_the_person() -> None:
    """At 22:00 UTC it is already the third in Omsk and still the second in
    Rio — the same achievement lands in different days for them."""
    now = datetime(2026, 9, 2, 22, 0, tzinfo=UTC)

    assert day_start_utc(5 * 60, now) == datetime(2026, 9, 2, 19, 0, tzinfo=UTC)
    assert day_start_utc(-3 * 60, now) == datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
    assert day_start_utc(None, now) == datetime(2026, 9, 2, 0, 0, tzinfo=UTC)


def test_month_boundary_follows_the_person() -> None:
    now = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    # For UTC-5 it is still August.
    assert month_start_utc(-5 * 60, now) == datetime(2026, 8, 1, 5, 0, tzinfo=UTC)
    assert month_start_utc(0, now) == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


async def test_counters_include_backfilled_rows(repo: Repo) -> None:
    """is_backfill means "do not publish", not "did not happen" (SPEC 5.9)."""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    await repo.insert_new_achievements(
        XUID,
        [row("old", "2026-08-15T10:00:00+00:00", 20)],
        is_backfill=True,
    )
    await repo.insert_new_achievements(
        XUID,
        [
            row("today", "2026-09-02T09:00:00+00:00", 15),
            row("yesterday", "2026-09-01T09:00:00+00:00", 5),
            row("undated", None, 50),
        ],
        is_backfill=False,
    )

    counters = await counters_for(repo, XUID, 0, now)

    assert (counters.today, counters.today_score) == (1, 15)
    assert (counters.month, counters.month_score) == (2, 20)
    # The undated one counts in the total and nowhere else.
    assert counters.total == 4


async def test_counts_by_xuid_covers_everyone_in_one_query(repo: Repo) -> None:
    await repo.insert_new_achievements(
        XUID, [row("a", "2026-09-02T09:00:00+00:00")], is_backfill=False
    )
    await repo.insert_new_achievements(
        "other", [row("b", "2026-09-02T09:00:00+00:00", 25)], is_backfill=False
    )

    counts = await repo.achievement_counts_by_xuid(datetime(2026, 9, 1, tzinfo=UTC))

    assert counts[XUID] == (1, 10)
    assert counts["other"] == (1, 25)
