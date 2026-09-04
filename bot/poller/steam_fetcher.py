"""Fetching Steam achievements and backfill (SPEC 9, M-Steam-2c/2d) — the
Steam counterpart of poller/fetcher.py. Smaller than the Xbox version: no
`ensure_title_name` equivalent (Steam's own presence already carries the
game's display name, `gameextrainfo` — SPEC 9, M-Steam-2c), and no title-
history refresh (no Steam analogue exists yet, scoped out of 2c on
purpose).
"""

from __future__ import annotations

import asyncio
import logging

from bot.db.repo import AchievementRow, Repo
from bot.poller.publisher import Publisher
from bot.services.models import ParsedAchievement
from bot.services.steam.achievements import fetch_unlocked
from bot.services.steam.client import OwnedGame, SteamApiError, get_owned_games

log = logging.getLogger(__name__)

# A backfill's per-game concurrency — Xbox never needed this second level
# (one call covers its whole library), Steam genuinely does since
# fetch_unlocked() is one call per game (SPEC 9, M-Steam-2d). Module
# constant, not a Settings field: internal tuning, not something the admin
# would ever need to reach for.
GAME_BACKFILL_CONCURRENCY = 5


class SteamFetcher:
    def __init__(
        self, repo: Repo, api_key: str, publisher: Publisher, concurrency: int = 2
    ) -> None:
        self._repo = repo
        self._api_key = api_key
        self._publisher = publisher
        self._backfill_slots = asyncio.Semaphore(concurrency)  # people backfilling at once
        self._game_slots = asyncio.Semaphore(GAME_BACKFILL_CONCURRENCY)  # games within one

    async def poll_title(
        self,
        tg_id: int,
        steam_id: str,
        persona_name: str,
        appid: str,
        game_name: str | None,
    ) -> int:
        """Fetch one game's achievements, keep the new ones, publish them."""
        parsed = await fetch_unlocked(self._repo, self._api_key, steam_id, appid)
        rows = [_to_row(item) for item in parsed]
        new_rows = await self._repo.insert_new_achievements_steam(
            tg_id, steam_id, rows, is_backfill=False
        )
        await self._repo.mark_steam_achievements_polled(steam_id)
        if not new_rows:
            return 0

        log.info("tg_id=%s unlocked %s new steam achievements in %s", tg_id, len(new_rows), appid)
        await self._publisher.publish(tg_id, steam_id, persona_name, new_rows, game_name)
        return len(new_rows)

    async def backfill(self, tg_id: int, steam_id: str) -> int:
        """Mark everything already unlocked as seen, publishing nothing —
        same principle as Xbox's backfill (SPEC 5.6), just spread over one
        request per played game instead of one call for the whole library
        (SPEC 9, M-Steam-2d: no Steam equivalent of Xbox's contract 2)."""
        async with self._backfill_slots:
            games = await get_owned_games(self._api_key, steam_id)
            rows: list[AchievementRow] = []

            async def one(game: OwnedGame) -> None:
                async with self._game_slots:
                    try:
                        parsed = await fetch_unlocked(
                            self._repo, self._api_key, steam_id, game.appid
                        )
                    except SteamApiError as exc:
                        log.info("steam backfill of appid=%s skipped: %s", game.appid, exc)
                        return
                    rows.extend(_to_row(item) for item in parsed)

            await asyncio.gather(*(one(game) for game in games))
            await self._repo.insert_new_achievements_steam(
                tg_id, steam_id, rows, is_backfill=True
            )
            log.info("steam backfill for tg_id=%s stored %s achievements", tg_id, len(rows))
            return len(rows)


def _to_row(item: ParsedAchievement) -> AchievementRow:
    return AchievementRow(
        title_id=item.title_id,
        achievement_id=item.achievement_id,
        name=item.name,
        description=item.description,
        icon_url=item.icon_url,
        unlocked_at=item.unlocked_at.isoformat(timespec="seconds") if item.unlocked_at else None,
        gamerscore=item.gamerscore,
        rarity_percent=item.rarity_percent,
        platform=item.platform,
        title_name=item.title_name,
        is_secret=item.is_secret,
    )
