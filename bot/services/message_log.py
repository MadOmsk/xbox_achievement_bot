"""Logs every message the bot sends to a group chat (SPEC 6.4).

Telegram gives a bot no way to list its own past messages in a chat — only
to delete one by a known message_id — so without a log of its own, "стереть
сообщения бота" in the admin panel would have nothing to work from.

Implemented as an aiogram request middleware, not a `repo.log_bot_message()`
call added to every place that sends something: this bot sends messages from
a dozen different handlers (chat.py, panel.py, admin.py, hltb.py), the
poller (daily.py, publisher.py) and the OAuth callback (main.py) — scattering
the call through all of them would be easy to miss on the next one added.
One middleware on the Bot's own request pipeline sees every outgoing call in
exactly one place, regardless of which handler made it.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.enums import ChatType
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Message

from bot.db.repo import Repo

log = logging.getLogger(__name__)

_GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


class MessageLogMiddleware(BaseRequestMiddleware):
    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> TelegramType:
        result = await make_request(bot, method)
        for message in _sent_messages(result):
            if message.chat.type in _GROUP_TYPES:
                with contextlib.suppress(Exception):
                    await self._repo.log_bot_message(message.chat.id, message.message_id)
        return result


def _sent_messages(result: object) -> list[Message]:
    if isinstance(result, Message):
        return [result]
    if isinstance(result, list) and result and isinstance(result[0], Message):
        return result
    return []
