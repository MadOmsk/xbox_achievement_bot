"""Notifications to the operator (SPEC 6.5).

Only events the admin can act on: someone joined, someone left, someone's login
died. Not a log — a log is in logs/bot.log, and a chat that reports every tick
gets muted, taking the useful messages with it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Bot

from bot.db.repo import Repo

log = logging.getLogger(__name__)


class AdminNotifier:
    def __init__(self, bot: Bot, repo: Repo, admin_ids: Sequence[int]) -> None:
        self._bot = bot
        self._repo = repo
        self._admin_ids = list(admin_ids)

    async def user_connected(self, tg_id: int, gamertag: str, *, is_new: bool) -> None:
        verb = "Добавлен пользователь" if is_new else "Переподключился"
        await self._send(f"➕ {verb}: {gamertag}\n{await self._who(tg_id)}")

    async def user_disconnected(self, tg_id: int, gamertag: str, reason: str) -> None:
        await self._send(f"➖ Отключился: {gamertag} ({reason})\n{await self._who(tg_id)}")

    async def token_dead(self, tg_id: int) -> None:
        user = await self._repo.get_user(tg_id)
        name = (user.gamertag if user else None) or f"id{tg_id}"
        await self._send(
            f"⚠️ У пользователя слетел вход: {name}\n{await self._who(tg_id)}\n"
            "Ачивки не публикуются, пока он не войдёт заново. Напоминание ему уже ушло."
        )

    async def _who(self, tg_id: int) -> str:
        user = await self._repo.get_user(tg_id)
        username = f"@{user.username}" if user and user.username else "без username"
        return f"tg_id {tg_id} · {username}"

    async def _send(self, text: str) -> None:
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text)
            except Exception:
                # An admin who blocked the bot must not break the flow that
                # triggered the notification.
                log.info("could not notify admin %s", admin_id)
