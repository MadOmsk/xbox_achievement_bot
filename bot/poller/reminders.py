"""Reminders about a dead login (SPEC 5.1.1)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.repo import Repo

log = logging.getLogger(__name__)

MAX_REMINDERS = 3
REMINDER_INTERVAL_HOURS = 72

TEXT = (
    "⚠️ Доступ к Xbox истёк\n\n"
    "Твои ачивки больше не публикуются. Обычно это значит, что доступ "
    "отозвали в настройках аккаунта Microsoft."
)


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Подключить заново", callback_data="relogin")],
            [InlineKeyboardButton(text="🔕 Отписаться от бота", callback_data="optout")],
        ]
    )


class ReminderJob:
    def __init__(self, bot: Bot, repo: Repo) -> None:
        self._bot = bot
        self._repo = repo

    async def run(self) -> None:
        candidates = await self._repo.tokens_needing_reminder(
            MAX_REMINDERS, REMINDER_INTERVAL_HOURS
        )
        for tg_id in candidates:
            try:
                await self._bot.send_message(tg_id, TEXT, reply_markup=keyboard())
            except TelegramForbiddenError:
                # Blocked the bot: stop counting attempts against him forever.
                log.info("tg_id=%s blocked the bot, no more reminders", tg_id)
                await self._repo.set_token_status(tg_id, "revoked")
                continue
            except Exception:
                log.exception("could not remind tg_id=%s", tg_id)
                continue
            await self._repo.mark_token_notified(tg_id)
