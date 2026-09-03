"""HowLongToBeat lookups (SPEC 6.6): search and resolve, cached forever.

No official API exists; the third-party howlongtobeatpy package reverse-
engineers HLTB's own search endpoint and tracks its (regularly changing)
obfuscation for us. Hidden behind this module so a break there, or a future
library swap, never touches handlers or the database schema beyond this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from howlongtobeatpy import HowLongToBeat

from bot.db.repo import HltbCacheRow, Repo

MAX_RESULTS = 20

# Xbox's own title names carry trademark clutter and separator punctuation
# HLTB's search doesn't expect — e.g. a chat-recent-games shortcut hands over
# "HELLDIVERS™ 2" verbatim (SPEC 6.6). Stripped to spaces, not deleted
# outright, so "Halo: Reach" still searches as two words, not "HaloReach".
_SEARCH_NOISE = re.compile(r"[™©®℠:,]")


class HltbError(Exception):
    """Expected failure — HLTB unreachable, or its layout changed underneath
    the library. Same "log and tell the user, never crash" treatment as an
    XboxApiError (SPEC 1.5)."""


@dataclass(slots=True)
class HltbResult:
    hltb_id: int
    name: str
    release_year: int | None
    main_hours: float | None
    extra_hours: float | None
    completionist_hours: float | None
    platforms: list[str]


async def search(query: str) -> list[HltbResult]:
    """Up to MAX_RESULTS candidates, best match first — nobody types an
    exact HLTB title, so the caller always needs to let a person pick
    (SPEC 6.6)."""
    query = _clean_query(query)
    try:
        # similarity_case_sensitive=False: found live — Xbox's own title
        # names are often ALL CAPS ("HELLDIVERS 2"), and the library's
        # default case-sensitive similarity check threw out the correct
        # match entirely (0 results) rather than just ranking it lower.
        entries = await HowLongToBeat().async_search(query, similarity_case_sensitive=False)
    except Exception as exc:  # the library exposes no narrower exception type
        raise HltbError(f"HLTB search failed for {query!r}: {exc}") from None
    entries = sorted(entries or [], key=lambda e: e.similarity, reverse=True)
    return [_as_result(e) for e in entries[:MAX_RESULTS]]


async def resolve(repo: Repo, hltb_id: int) -> HltbResult:
    """Cached forever once a person actually picks a result — a completion
    time does not meaningfully change day to day, and HLTB's search is the
    fragile part here, not this number (SPEC 6.6)."""
    cached = await repo.hltb_get_cached(hltb_id)
    if cached is not None:
        return _from_cache_row(cached)

    try:
        entry = await HowLongToBeat().async_search_from_id(hltb_id)
    except Exception as exc:
        raise HltbError(f"HLTB lookup failed for id={hltb_id}: {exc}") from None
    if entry is None:
        raise HltbError(f"HLTB has no entry for id={hltb_id}")

    result = _as_result(entry)
    await repo.hltb_cache_result(
        HltbCacheRow(
            hltb_id=result.hltb_id,
            name=result.name,
            release_year=result.release_year,
            main_hours=result.main_hours,
            extra_hours=result.extra_hours,
            completionist_hours=result.completionist_hours,
            platforms=result.platforms,
        )
    )
    return result


def _as_result(entry: object) -> HltbResult:
    return HltbResult(
        hltb_id=entry.game_id,  # type: ignore[attr-defined]
        name=entry.game_name,  # type: ignore[attr-defined]
        release_year=entry.release_world,  # type: ignore[attr-defined]
        main_hours=_clean(entry.main_story),  # type: ignore[attr-defined]
        extra_hours=_clean(entry.main_extra),  # type: ignore[attr-defined]
        completionist_hours=_clean(entry.completionist),  # type: ignore[attr-defined]
        platforms=list(entry.profile_platforms or []),  # type: ignore[attr-defined]
    )


def _from_cache_row(row: HltbCacheRow) -> HltbResult:
    return HltbResult(
        hltb_id=row.hltb_id,
        name=row.name,
        release_year=row.release_year,
        main_hours=row.main_hours,
        extra_hours=row.extra_hours,
        completionist_hours=row.completionist_hours,
        platforms=row.platforms,
    )


def _clean(hours: float | None) -> float | None:
    # Some entries (co-op/PvP-only games) report 0 or omit a style entirely —
    # "0 hours" would read as an error, not as "no data".
    return hours if hours and hours > 0 else None


def _clean_query(text: str) -> str:
    return " ".join(_SEARCH_NOISE.sub(" ", text).split())
