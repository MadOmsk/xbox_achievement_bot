"""Counters and day boundaries (SPEC 5.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.db.repo import AchievementRow, Repo
from bot.services.stats import counters_for, month_cutoff_utc, today_cutoff_utc

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


async def _link(repo: Repo, tg_id: int, xuid: str) -> None:
    """insert_new_achievements now resolves tg_id from xuid (SPEC 9,
    M-Steam-2) — a row needs a real linked user behind its xuid."""
    await repo.ensure_user(tg_id, f"user{tg_id}")
    await repo.link_xbox_account(tg_id, xuid, f"Player{tg_id}", 0)


def test_today_is_a_rolling_24_hours_not_a_calendar_day() -> None:
    """No timezone parameter on purpose: everyone's "today" is the same 24
    hours, unlike a calendar day (SPEC 5.9, matching the summary's window)."""
    now = datetime(2026, 9, 2, 22, 0, tzinfo=UTC)
    assert today_cutoff_utc(now) == now - timedelta(hours=24)
    assert today_cutoff_utc(now) == datetime(2026, 9, 1, 22, 0, tzinfo=UTC)


def test_month_is_a_rolling_30_days_not_a_calendar_month() -> None:
    """No timezone parameter, same as today_cutoff_utc — a mismatched window
    against /stats' equally-30-day games table is what made this look like a
    counting bug rather than two different definitions of "month" (SPEC 5.9)."""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    assert month_cutoff_utc(now) == now - timedelta(days=30)
    assert month_cutoff_utc(now) == datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


async def test_counters_include_backfilled_rows(repo: Repo) -> None:
    """is_backfill means "do not publish", not "did not happen" (SPEC 5.9)."""
    await _link(repo, 1, XUID)
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    await repo.insert_new_achievements(
        XUID,
        # 18 days before `now` — inside the 30-day rolling "month" window.
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

    counters = await counters_for(repo, XUID, now)

    assert (counters.today, counters.today_score) == (1, 15)
    assert (counters.month, counters.month_score) == (3, 40)
    # Counters has no lifetime total (SPEC 5.4, best-effort forever) — but
    # the repo-level, unbounded count (used only by the reconciliation
    # script, never displayed) still sees the backfilled and undated rows.
    assert await repo.achievement_counts(XUID, None) == (4, 90)


async def test_counters_today_crosses_midnight_correctly(repo: Repo) -> None:
    """23 hours ago is still "today" even though it was yesterday on the
    calendar — and 25 hours ago is not, even on the same calendar day."""
    await _link(repo, 1, XUID)
    now = datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
    await repo.insert_new_achievements(
        XUID,
        [
            row("late-yesterday", "2026-09-01T02:00:00+00:00", 10),  # 23h ago
            row("early-today", "2026-09-02T00:30:00+00:00", 20),  # 30m ago
        ],
        is_backfill=False,
    )

    counters = await counters_for(repo, XUID, now)

    assert counters.today == 2
    assert counters.today_score == 30


async def test_counts_by_xuid_covers_everyone_in_one_query(repo: Repo) -> None:
    await _link(repo, 1, XUID)
    await _link(repo, 2, "other")
    await repo.insert_new_achievements(
        XUID, [row("a", "2026-09-02T09:00:00+00:00")], is_backfill=False
    )
    await repo.insert_new_achievements(
        "other", [row("b", "2026-09-02T09:00:00+00:00", 25)], is_backfill=False
    )

    counts = await repo.achievement_counts_by_xuid(datetime(2026, 9, 1, tzinfo=UTC))

    assert counts[XUID] == (1, 10)
    assert counts["other"] == (1, 25)


async def test_recent_achievements_orders_newest_first_and_respects_limit(
    repo: Repo,
) -> None:
    await _link(repo, 1, XUID)
    await repo.insert_new_achievements(
        XUID,
        [
            row("first", "2026-09-01T10:00:00+00:00"),
            row("second", "2026-09-02T10:00:00+00:00"),
            row("third", "2026-09-03T10:00:00+00:00"),
            row("undated", None),  # never wins a "recent" slot
        ],
        is_backfill=False,
    )

    recent = await repo.recent_achievements(XUID, limit=2)

    assert [item.achievement_id for item in recent] == ["third", "second"]


async def test_insert_for_an_unlinked_xuid_is_dropped_not_crashed(repo: Repo) -> None:
    """No `users` row has this xuid at all — insert_new_achievements resolves
    tg_id from it (SPEC 9, M-Steam-2) and must not crash when that lookup
    comes up empty; it just drops the rows (should never happen in practice,
    an xuid always comes from a connected user)."""
    new_rows = await repo.insert_new_achievements(
        "no-such-xuid", [row("a", "2026-09-02T09:00:00+00:00")], is_backfill=False
    )
    assert new_rows == []
