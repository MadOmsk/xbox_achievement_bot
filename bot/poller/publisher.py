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

from bot.db.repo import AchievementRow, Repo
from bot.services.achievements import format_digest, format_single, passes_filters

log = logging.getLogger(__name__)

SEND_INTERVAL_SECONDS = 3.0  # ~20 messages a minute


@dataclass(slots=True)
class PublishJob:
    chat_id: int
    xuid: str
    text: str
    photo_url: str | None
    # Only meaningful with photo_url set — a secret achievement's icon
    # (SPEC 5.5, 7.1) can be as much of a spoiler as its name.
    photo_has_spoiler: bool = False
    items: list[tuple[str, str]] = field(default_factory=list)  # (title_id, achievement_id)


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

        user_settings = await self._repo.get_user_settings(tg_id)
        if user_settings is None:
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
            # what one chat sees as five achievements may be one in another.
            if len(allowed) >= user_settings.digest_threshold:
                await self._queue.put(
                    PublishJob(
                        chat_id=chat.chat_id,
                        xuid=xuid,
                        text=format_digest(gamertag, title_name, allowed),
                        photo_url=None,
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
                        photo_url=item.icon_url,
                        photo_has_spoiler=item.is_secret,
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
        if job.photo_url:
            try:
                message = await self._bot.send_photo(
                    job.chat_id,
                    photo=job.photo_url,
                    caption=job.text,
                    parse_mode=ParseMode.HTML,
                    has_spoiler=job.photo_has_spoiler,
                )
                return message.message_id
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception:
                # The achievement matters more than the picture (SPEC 7.1).
                log.info("icon for chat %s did not go through, sending text", job.chat_id)
        message = await self._bot.send_message(job.chat_id, job.text, parse_mode=ParseMode.HTML)
        return message.message_id
