"""Steam achievements: fetch + cache, composing over services/steam/client.py
(SPEC 9, M-Steam-2b). Mirrors services/hltb.py's role in the project — a
cache-backed service layer sitting above a stateless client, the same split
hltb.py already uses, rather than either putting caching directly in
client.py or waiting until the poller layer (which is Xbox's own pattern,
services/xbox/client.py + poller/fetcher.py).

Errors from the client (SteamApiError — private profile, unreachable API,
bad key) are not caught here on purpose: this is a pure fetch, the same
"log it and skip this tick" handling XboxApiError already gets belongs to
the poller that calls this (SPEC 9, M-Steam-2c), not to this layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.db.repo import Repo, SteamSchemaAchievement
from bot.services.models import ParsedAchievement
from bot.services.steam.client import get_global_percentages, get_player_achievements, get_schema
from bot.util import parse_iso, utcnow

# "Раз в неделю" (SPEC 9, M-Steam-2b) — real unlock percentages drift slowly,
# and re-fetching more often than this buys nothing (no key-less rate limit
# to worry about either), while never re-fetching would leave old games'
# rarity permanently stale.
RARITY_CACHE_TTL_DAYS = 7


async def fetch_unlocked(
    repo: Repo, api_key: str, steam_id: str, appid: str
) -> list[ParsedAchievement]:
    """Every currently-unlocked achievement for one Steam game. Writing to
    seen_achievements and resolving tg_id are the poller's job (SPEC 9,
    M-Steam-2c) — this only ever reads the Steam API and this module's own
    two cache tables."""
    raw = await get_player_achievements(api_key, steam_id, appid)
    unlocked = [item for item in raw if item.achieved]
    if not unlocked:
        # Nothing achieved yet, or a private/stats-less profile (client.py
        # already turned Steam's own `success: false` into an empty list) —
        # either way there is nothing worth a schema/rarity lookup for.
        return []

    schema_by_id = {a.apiname: a for a in await _schema(repo, api_key, appid)}
    percentages = await _percentages(repo, appid)

    result: list[ParsedAchievement] = []
    for item in unlocked:
        schema_item = schema_by_id.get(item.apiname)
        result.append(
            ParsedAchievement(
                achievement_id=item.apiname,
                title_id=appid,
                title_name=None,  # presence already has it fresh (SPEC 9, M-Steam-2c)
                name=item.name,
                description=item.description,
                icon_url=schema_item.icon if schema_item else None,
                unlocked_at=_parse_unlocktime(item.unlocktime),
                gamerscore=0,  # Steam has no gamerscore — SPEC 9, M-Steam-2e keeps it Xbox-only
                rarity_percent=percentages.get(item.apiname),
                platform="steam",
                is_secret=schema_item.hidden if schema_item else False,
            )
        )
    return result


async def _schema(repo: Repo, api_key: str, appid: str) -> list[SteamSchemaAchievement]:
    cached = await repo.steam_schema_get_cached(appid)
    if cached is not None:
        return cached[1]
    raw = await get_schema(api_key, appid)
    achievements = [
        SteamSchemaAchievement(apiname=item.apiname, icon=item.icon, hidden=item.hidden)
        for item in raw
    ]
    await repo.steam_schema_cache_result(appid, None, achievements)
    return achievements


async def _percentages(repo: Repo, appid: str) -> dict[str, float]:
    cached = await repo.steam_rarity_get_cached(appid)
    if cached is not None:
        percentages, cached_at = cached
        cached_dt = parse_iso(cached_at)
        if cached_dt is not None and utcnow() - cached_dt < timedelta(days=RARITY_CACHE_TTL_DAYS):
            return percentages

    percentages = await get_global_percentages(appid)
    await repo.steam_rarity_cache_result(appid, percentages)
    return percentages


def _parse_unlocktime(unlocktime: int) -> datetime | None:
    # 0 is Steam's own "no real date" placeholder — same class of problem as
    # Xbox's 0001-01-01/1753-01-01 (bot/services/xbox/models.py), just a
    # unix-epoch int instead of an ISO string, so parse_timestamp there
    # doesn't apply directly; same "placeholder means unknown, not a date"
    # principle, its own small converter.
    return datetime.fromtimestamp(unlocktime, tz=UTC) if unlocktime > 0 else None
