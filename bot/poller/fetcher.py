"""Fetching achievements, deduplication and backfill (SPEC 5.3, 5.4, 5.6)."""

from __future__ import annotations

import asyncio
import logging

from bot.db.repo import AchievementRow, Repo, TitleHistoryRow
from bot.poller.publisher import Publisher
from bot.services.xbox.client import XboxApiError, XboxClient
from bot.services.xbox.models import ParsedAchievement, Platform

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(
        self, repo: Repo, client: XboxClient, publisher: Publisher, concurrency: int = 2
    ) -> None:
        self._repo = repo
        self._client = client
        self._publisher = publisher
        self._backfill_slots = asyncio.Semaphore(concurrency)

    async def poll_title(
        self,
        tg_id: int,
        xuid: str,
        gamertag: str,
        title_id: str,
        platform: Platform,
        title_name: str | None,
    ) -> int:
        """Fetch one game's achievements, keep the new ones, publish them."""
        parsed = await self._client.title_achievements(tg_id, title_id, platform)
        rows = [_to_row(item) for item in parsed]
        new_rows = await self._repo.insert_new_achievements(xuid, rows, is_backfill=False)
        await self._repo.mark_achievements_polled(xuid)
        if not new_rows:
            return 0

        log.info("tg_id=%s unlocked %s new achievements in %s", tg_id, len(new_rows), title_id)
        stored_name = title_name or await self._repo.title_name(title_id)
        await self._publisher.publish(tg_id, xuid, gamertag, new_rows, stored_name)
        return len(new_rows)

    async def backfill(self, tg_id: int, xuid: str) -> int:
        """Mark everything already unlocked as seen, publishing nothing.

        Without this the first poll after connecting would dump thousands of
        old achievements into the chat.
        """
        async with self._backfill_slots:
            rows = [_to_row(item) for item in await self._client.all_achievements(tg_id)]

            # Contract 2 covers modern titles only — verified against a live
            # account, where an Xbox 360 game with 33 unlocked achievements was
            # absent from the full list. Without this second pass the first
            # session in such a game would look like 33 fresh unlocks.
            history = await self._client.title_history(tg_id)
            for entry in history:
                if entry.platform != "x360":
                    continue
                try:
                    parsed = await self._client.title_achievements(tg_id, entry.title_id, "x360")
                except XboxApiError as exc:
                    log.info("x360 backfill of %s skipped: %s", entry.title_id, exc)
                    continue
                rows.extend(_to_row(item) for item in parsed)

            await self._repo.insert_new_achievements(xuid, rows, is_backfill=True)
            await self._save_history(tg_id, xuid, history)
            log.info("backfill for tg_id=%s stored %s achievements", tg_id, len(rows))
            return len(rows)

    async def refresh_title_history(self, tg_id: int, xuid: str) -> None:
        """Source of /stats, /top and of the gamerscore in the panel (SPEC 5.4)."""
        await self._save_history(tg_id, xuid, await self._client.title_history(tg_id))

    async def _save_history(self, tg_id: int, xuid: str, history: list) -> None:
        rows = [
            TitleHistoryRow(
                title_id=entry.title_id,
                name=entry.name,
                platform=entry.platform,
                current_gamerscore=entry.current_gamerscore,
                max_gamerscore=entry.max_gamerscore,
                achievements_unlocked=entry.achievements_unlocked,
                achievements_total=entry.achievements_total,
                last_played_at=entry.last_played_at,
            )
            for entry in history
        ]
        if not rows:
            return
        await self._repo.save_title_history(xuid, rows)
        await self._repo.update_gamerscore(tg_id, sum(row.current_gamerscore or 0 for row in rows))


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
    )
