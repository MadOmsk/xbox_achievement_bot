"""Auto-delete for "system" group messages (2026-09-05 follow-up).

A group chat's bot messages fall into two piles (bot/services/message_log.py's
own `is_system` flag): the "stats" results people come back to read
(/stats, /recent, /summary + the daily итог, achievement messages, /online,
/hltb's game card) and everything else — prompts, confirmations, /help, the
group hub. The second pile is meant to be transient, so this tick removes
whatever has sat past the configured TTL, admin-wide (SPEC ..., not per chat —
there is one clock for the whole bot, same as e.g. `default_rarity_mode`).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot

from bot.db.repo import Repo
from bot.util import utcnow

log = logging.getLogger(__name__)

DEFAULT_TTL_MINUTES = 5
TTL_SETTING_KEY = "system_message_ttl_min"


async def system_message_ttl_minutes(repo: Repo) -> int:
    raw = await repo.get_app_setting(TTL_SETTING_KEY, str(DEFAULT_TTL_MINUTES))
    try:
        return int(raw or DEFAULT_TTL_MINUTES)
    except ValueError:
        return DEFAULT_TTL_MINUTES


class MessageCleanup:
    def __init__(self, bot: Bot, repo: Repo) -> None:
        self._bot = bot
        self._repo = repo

    async def tick(self) -> None:
        ttl = await system_message_ttl_minutes(self._repo)
        if ttl <= 0:
            return  # 0 = the admin's own off switch
        cutoff = utcnow() - timedelta(minutes=ttl)
        due = await self._repo.due_system_messages(cutoff)
        for chat_id, message_id in due:
            try:
                await self._bot.delete_message(chat_id, message_id)
            except Exception:
                # Expected, not exceptional: someone already deleted it, the
                # bot got kicked, or it's past Telegram's 48h delete window —
                # same reasoning as /delete_last and the admin panel's own
                # bulk wipe (chat.py, admin.py). Nothing left worth retrying,
                # so the row is dropped either way, below.
                log.info("system message cleanup: could not delete %s/%s", chat_id, message_id)
            await self._repo.forget_bot_messages(chat_id, [message_id])
