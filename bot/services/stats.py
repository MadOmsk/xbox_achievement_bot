"""Aggregates for the panels, /stats, /top and the daily summary (SPEC 5.9).

Everything is counted from `seen_achievements.unlocked_at` regardless of
`is_backfill`: that flag means "do not publish", not "did not happen".

Day and month boundaries depend on whose numbers these are — a person sees his
own day in his own timezone, while a chat-wide table uses one common offset, or
its rows would not add up.
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
    total: int = 0
    total_score: int = 0


def local_now(tz_offset_min: int | None, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(minutes=tz_offset_min or 0)


def day_start_utc(tz_offset_min: int | None, now: datetime | None = None) -> datetime:
    """Midnight of the person's day, expressed in UTC."""
    local = local_now(tz_offset_min, now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(minutes=tz_offset_min or 0)


def month_start_utc(tz_offset_min: int | None, now: datetime | None = None) -> datetime:
    local = local_now(tz_offset_min, now)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first - timedelta(minutes=tz_offset_min or 0)


async def counters_for(
    repo: Repo, xuid: str, tz_offset_min: int | None, now: datetime | None = None
) -> Counters:
    today, today_score = await repo.achievement_counts(xuid, day_start_utc(tz_offset_min, now))
    month, month_score = await repo.achievement_counts(xuid, month_start_utc(tz_offset_min, now))
    total, total_score = await repo.achievement_counts(xuid, None)
    return Counters(today, today_score, month, month_score, total, total_score)


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
