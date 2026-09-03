"""Aggregates for the panels, /stats, /top and the daily summary (SPEC 5.9).

Everything is counted from `seen_achievements.unlocked_at` regardless of
`is_backfill`: that flag means "do not publish", not "did not happen".

"Today" is a rolling 24 hours everywhere in the project, not a calendar day —
the same reasoning as the chat summary's window (SPEC 5.7): a calendar
boundary cuts at an arbitrary moment, and people are in different timezones
with no shared midnight anyway. "Month" is still calendar-based per person's
own timezone; only the day boundary moved to rolling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


def month_start_utc(tz_offset_min: int | None, now: datetime | None = None) -> datetime:
    local = local_now(tz_offset_min, now)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first - timedelta(minutes=tz_offset_min or 0)


async def counters_for(
    repo: Repo, xuid: str, tz_offset_min: int | None, now: datetime | None = None
) -> Counters:
    today, today_score = await repo.achievement_counts(xuid, today_cutoff_utc(now))
    month, month_score = await repo.achievement_counts(xuid, month_start_utc(tz_offset_min, now))
    return Counters(today, today_score, month, month_score)


async def global_offset_minutes(repo: Repo) -> int:
    """Offset used where one common day boundary is needed (SPEC 5.9).

    Stored as an IANA name, but the whole bot works in fixed offsets, so it is
    resolved once here rather than dragging tzdata through every counter.
    """
    name = await repo.get_app_setting("timezone")
    if not name:
        return 0
    try:
        from zoneinfo import ZoneInfo

        offset = datetime.now(ZoneInfo(name)).utcoffset()
    except Exception:
        return 0
    return int(offset.total_seconds() // 60) if offset else 0


def as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
