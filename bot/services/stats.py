"""Aggregates for the panels, /stats, /top and the daily summary (SPEC 5.9).

Everything is counted from `seen_achievements.unlocked_at` regardless of
`is_backfill`: that flag means "do not publish", not "did not happen".

Both "today" (24h) and "month" (30d) are rolling windows, not calendar-bound
— the same reasoning as the chat summary's windows (SPEC 5.7): a calendar
boundary cuts at an arbitrary moment, and people are in different timezones
with no shared midnight anyway. Month used to be calendar-based per person's
timezone; that made it silently disagree with /stats' equally-30-day "Игры
за 30 дней" table (found live: a chat member's games table showed a month's
worth of games while "За месяц" showed three days' worth, because 30
calendar-rolling days and "since the 1st" are not the same window) — fixed
by making both counters and the games table use the same rolling window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.db.repo import Repo
from bot.util import utcnow


@dataclass(slots=True)
class Counters:
    today: int = 0
    today_score: int = 0
    month: int = 0
    month_score: int = 0
    # No lifetime total: seen_achievements is permanently best-effort (a
    # capped title_history, achievements with no unlock date), unlike these
    # two date-bounded counts — better absent than quietly wrong (SPEC 5.4).


def local_now(tz_offset_min: int | None, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(minutes=tz_offset_min or 0)


def today_cutoff_utc(now: datetime | None = None) -> datetime:
    """Start of the rolling 24-hour "today" window.

    No timezone parameter: a rolling window does not need one, and that is
    the point — everyone's "today" is the same 24 hours, unlike a calendar day.
    """
    return (now or utcnow()) - timedelta(hours=24)


def month_cutoff_utc(now: datetime | None = None) -> datetime:
    """Start of the rolling 30-day "month" window — same reasoning as
    `today_cutoff_utc`, and deliberately the same window as /stats' "Игры
    за 30 дней" table (SPEC 5.9): a mismatched window there is what made
    this look like a counting bug rather than two different definitions of
    "month"."""
    return (now or utcnow()) - timedelta(days=30)


async def counters_for(repo: Repo, tg_id: int, now: datetime | None = None) -> Counters:
    """Summed across every platform the person has connected (SPEC 9,
    M-Steam-2e) — keyed by tg_id, not any one platform's own external id."""
    today, today_score = await repo.achievement_counts_for_person(tg_id, today_cutoff_utc(now))
    month, month_score = await repo.achievement_counts_for_person(tg_id, month_cutoff_utc(now))
    return Counters(today, today_score, month, month_score)
