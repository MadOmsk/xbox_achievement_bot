"""Fetching achievements, deduplication and backfill (SPEC 5.3, 5.4, 5.6)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from bot.db.repo import AchievementRow, Repo, TitleHistoryRow
from bot.poller.publisher import Publisher
from bot.services.xbox.client import TitleHistoryEntry, XboxApiError, XboxClient
from bot.services.xbox.models import ParsedAchievement, Platform
from bot.util import parse_iso, utcnow

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
        resolved = await self._title_name(tg_id, title_id, title_name)
        await self._publisher.publish(tg_id, xuid, gamertag, new_rows, resolved)
        return len(new_rows)

    async def _title_name(self, tg_id: int, title_id: str, from_presence: str | None) -> str | None:
        """Presence leaves the name empty for PC titles, so ask titlehub once.

        "неизвестная игра" in a published message is worse than one extra
        request per game we have never seen.
        """
        if from_presence:
            return from_presence
        cached = await self._repo.title_name(title_id)
        if cached:
            return cached
        try:
            entry = await self._client.resolve_title(tg_id, title_id)
        except XboxApiError as exc:
            log.info("could not resolve title %s: %s", title_id, exc)
            return None
        if entry is None or not entry.name:
            return None
        await self._repo.upsert_title(entry.title_id, entry.name, entry.platform)
        return entry.name

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

    async def catch_up(
        self,
        tg_id: int,
        xuid: str,
        gamertag: str,
        since: datetime | None,
        window_hours: int,
        max_titles: int,
    ) -> tuple[int, int]:
        """Pick up what was unlocked while the bot was down (SPEC 5.8).

        Only achievements newer than the window reach the chat. Older ones are
        still recorded, just not announced: after a fortnight of downtime a
        chat does not want the archive, and after a one-minute restart nothing
        should be lost.
        """
        async with self._backfill_slots:
            history = await self._client.title_history(tg_id)
            await self._save_history(tg_id, xuid, history)

            candidates = _played_since(history, since)[:max_titles]
            if not candidates:
                return 0, 0

            publish_after = utcnow() - timedelta(hours=window_hours)
            published = 0
            for entry in candidates:
                try:
                    parsed = await self._client.title_achievements(
                        tg_id, entry.title_id, entry.platform
                    )
                except XboxApiError as exc:
                    log.info("catch-up skipped title %s: %s", entry.title_id, exc)
                    continue

                new_rows = await self._repo.insert_new_achievements(
                    xuid, [_to_row(item) for item in parsed], is_backfill=False
                )
                fresh = [row for row in new_rows if _unlocked_after(row, publish_after)]
                if fresh:
                    await self._publisher.publish(tg_id, xuid, gamertag, fresh, entry.name)
                    published += len(fresh)

            log.info(
                "catch-up for tg_id=%s: %s titles, %s published",
                tg_id,
                len(candidates),
                published,
            )
            return len(candidates), published

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

        # From the profile, not from the sum above: the title history request is
        # capped, so an account with more games than the cap would show too low
        # a score.
        try:
            total = await self._client.gamerscore(tg_id)
        except XboxApiError as exc:
            log.info("gamerscore for tg_id=%s not refreshed: %s", tg_id, exc)
            return
        if total is not None:
            await self._repo.update_gamerscore(tg_id, total)


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


def _played_since(
    history: list[TitleHistoryEntry], since: datetime | None
) -> list[TitleHistoryEntry]:
    """Games touched after our last look, most recent first."""
    entries = [(parse_iso(entry.last_played_at), entry) for entry in history]
    fresh = [
        (played, entry)
        for played, entry in entries
        if played is not None and (since is None or played > since)
    ]
    fresh.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in fresh]


def _unlocked_after(row: AchievementRow, moment: datetime) -> bool:
    unlocked = parse_iso(row.unlocked_at)
    # An unknown date is not proof of freshness — those stay unpublished.
    return unlocked is not None and unlocked >= moment
