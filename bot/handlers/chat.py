"""Group commands. M2 covers /subscribe and /unsubscribe (SPEC 6.3)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

from bot.db.repo import Repo

log = logging.getLogger(__name__)

router = Router(name="chat")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


class UsernameMiddleware(BaseMiddleware):
    """Keep users.username fresh — Bot API cannot resolve @name on demand.

    Only existing rows are touched: a group member who never talked to the bot
    should not get a user record just for writing a message.
    """

    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user and event.from_user.username:
            await self._repo.update_username(event.from_user.id, event.from_user.username)
        return await handler(event, data)


@router.message(Command("subscribe"))
async def subscribe(message: Message, repo: Repo) -> None:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Эта команда для группового чата — там, где нужны публикации.")
        return
    if message.from_user is None:
        return

    user = await repo.get_user(message.from_user.id)
    if user is None or not user.xuid:
        me = await message.bot.me()  # type: ignore[union-attr]
        await message.answer(
            f"Сначала подключи Xbox в личке: https://t.me/{me.username}?start=connect"
        )
        return

    await repo.upsert_chat(message.chat.id, message.chat.title, message.from_user.id)
    if await repo.is_subscribed(message.chat.id, message.from_user.id):
        await message.answer("Ты уже публикуешься здесь.")
        return

    await repo.subscribe(message.chat.id, message.from_user.id)
    await message.answer(
        f"Готово. Ачивки {user.gamertag or 'твои'} будут прилетать сюда.\n"
        "Настройки редкости и Xbox 360 — в личке, /panel."
    )


@router.message(Command("unsubscribe"))
async def unsubscribe(message: Message, repo: Repo) -> None:
    if message.chat.type not in GROUP_TYPES or message.from_user is None:
        return
    if not await repo.is_subscribed(message.chat.id, message.from_user.id):
        await message.answer("Ты здесь и не публиковался.")
        return
    await repo.unsubscribe(message.chat.id, message.from_user.id)
    await message.answer("Больше не публикую твои ачивки в этом чате.")
