"""Publishing to Telegram: filtering, digest, queue (SPEC 5.5).

Telegram tolerates about 20 messages per minute into one group, so everything
goes through a queue with a delay. Nothing here talks to Xbox Live.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto

from bot.db.repo import AchievementRow, Repo
from bot.services.achievements import format_digest, format_single, passes_filters

log = logging.getLogger(__name__)

SEND_INTERVAL_SECONDS = 3.0  # ~20 messages a minute

# Telegram's own cap on one media group (sendMediaGroup) — a digest with more
# achievements than this still lists every one of them in the text (SPEC
# 7.2), the gallery is just illustrative, not required to be exhaustive.
MEDIA_GROUP_MAX = 10


@dataclass(slots=True)
class PublishJob:
    chat_id: int
    xuid: str
    text: str
    # (icon_url, is_secret) per achievement, in order — a single achievement
    # is just a one-item gallery here, not a separate field any more
    # (2026-09-05 follow-up, SPEC 7.1/7.2): one delivery path for both
    # instead of two that used to duplicate each other's fallback-to-text
    # handling. Rows with no icon at all are dropped before this point, not
    # here — an empty list means "no photo, plain text".
    gallery: list[tuple[str, bool]] = field(default_factory=list)
    items: list[tuple[str, str]] = field(default_factory=list)  # (title_id, achievement_id)


def _gallery(achievements: list[AchievementRow]) -> list[tuple[str, bool]]:
    """One gallery entry per *distinct* icon, not per achievement — Xbox 360
    achievements all share the same icon (the game's own box art, no
    per-achievement art exists at all — SPEC 7.1), and a digest of several
    x360 unlocks would otherwise repeat that one picture N times. Order
    follows first appearance; an achievement sharing an already-seen icon
    still forces that icon's spoiler on if it itself is secret, so a
    same-icon secret never rides in unmarked behind an earlier public one.
    """
    order: list[str] = []
    has_spoiler: dict[str, bool] = {}
    for item in achievements:
        if not item.icon_url:
            continue
        if item.icon_url not in has_spoiler:
            order.append(item.icon_url)
            has_spoiler[item.icon_url] = item.is_secret
        elif item.is_secret:
            has_spoiler[item.icon_url] = True
    return [(url, has_spoiler[url]) for url in order]


class Publisher:
    def __init__(self, bot: Bot, repo: Repo) -> None:
        self._bot = bot
        self._repo = repo
        self._queue: asyncio.Queue[PublishJob] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def publish(
        self,
        tg_id: int,
        xuid: str,
        gamertag: str,
        achievements: list[AchievementRow],
        title_name: str | None = None,
    ) -> None:
        """Decide per chat what to send, then hand it to the queue."""
        if not achievements:
            return

        for chat in await self._repo.publication_targets(tg_id):
            allowed = [
                item
                for item in achievements
                if passes_filters(item, chat, chat.rare_threshold_percent)
            ]
            if not allowed:
                continue

            # The digest decision is per chat and happens after filtering:
            # what one chat sees as five achievements may be one in another
            # (digest_threshold lives on the subscription now, not on
            # user_settings — Follow-up, 2026-09-05, same move as
            # rarity_mode before it).
            if len(allowed) >= chat.digest_threshold:
                await self._queue.put(
                    PublishJob(
                        chat_id=chat.chat_id,
                        xuid=xuid,
                        text=format_digest(gamertag, title_name, allowed),
                        gallery=_gallery(allowed),
                        items=[(a.title_id, a.achievement_id) for a in allowed],
                    )
                )
                continue

            for item in allowed:
                if await self._repo.is_published(
                    chat.chat_id, xuid, item.title_id, item.achievement_id
                ):
                    continue
                await self._queue.put(
                    PublishJob(
                        chat_id=chat.chat_id,
                        xuid=xuid,
                        text=format_single(gamertag, item, title_name),
                        gallery=_gallery([item]),
                        items=[(item.title_id, item.achievement_id)],
                    )
                )

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._send(job)
            except Exception:
                log.exception("failed to publish into chat %s", job.chat_id)
            finally:
                self._queue.task_done()
            await asyncio.sleep(SEND_INTERVAL_SECONDS)

    async def _send(self, job: PublishJob) -> None:
        try:
            message_id = await self._deliver(job)
        except TelegramForbiddenError:
            # Kicked out of the group — stop trying forever (SPEC 5.5).
            log.info("chat %s is not available any more, deactivating", job.chat_id)
            await self._repo.deactivate_chat(job.chat_id)
            return
        except TelegramRetryAfter as exc:
            log.info("Telegram asked to wait %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            message_id = await self._deliver(job)

        for title_id, achievement_id in job.items:
            await self._repo.record_publication(
                job.chat_id, job.xuid, title_id, achievement_id, message_id
            )

    async def _deliver(self, job: PublishJob) -> int | None:
        # The achievement matters more than the picture(s) (SPEC 7.1) — any
        # failure below falls through to plain text rather than losing the
        # achievement, same principle at every step: gallery, then a single
        # photo, then text.
        if len(job.gallery) >= 2:
            try:
                media = [
                    InputMediaPhoto(
                        media=url,
                        has_spoiler=secret,
                        caption=job.text if index == 0 else None,
                        parse_mode=ParseMode.HTML if index == 0 else None,
                    )
                    for index, (url, secret) in enumerate(job.gallery[:MEDIA_GROUP_MAX])
                ]
                messages = await self._bot.send_media_group(job.chat_id, media)
                return messages[0].message_id if messages else None
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception:
                log.info("gallery for chat %s did not go through, sending text", job.chat_id)
        elif len(job.gallery) == 1:
            url, secret = job.gallery[0]
            try:
                message = await self._bot.send_photo(
                    job.chat_id, photo=url, caption=job.text,
                    parse_mode=ParseMode.HTML, has_spoiler=secret,
                )
                return message.message_id
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception:
                log.info("icon for chat %s did not go through, sending text", job.chat_id)

        message = await self._bot.send_message(job.chat_id, job.text, parse_mode=ParseMode.HTML)
        return message.message_id
