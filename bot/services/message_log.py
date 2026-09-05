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

Every logged row also carries is_system (2026-09-05 follow-up, "system
message" auto-delete, poller/message_cleanup.py) — 1 for an intermediate
message (a prompt, a confirmation, /help, the group hub), 0 for one of the
"stats" results (/stats, /recent, /summary + the daily итог, achievement
messages, /online, /hltb's game card) that must never disappear on its own.
The middleware has no idea which handler is calling or why, so the handful
of call sites that produce a "stats" result wrap their own send in
`stats_category()` below — a ContextVar, because the actual `bot.
send_message`/`.answer()` this middleware sees happens several layers below
the handler that decided the category, too far to thread a parameter
through cleanly.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

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

_stats_category: ContextVar[bool] = ContextVar("stats_category_message", default=False)


@contextmanager
def stats_category() -> Iterator[None]:
    """Wrap a send call that produces a "stats" result — see the module
    docstring for the exact list. Defaults to off (system) outside this
    context manager, on purpose: a call site that forgets to wrap itself
    fails safe by having its message disappear early, not by leaking a
    prompt into the never-deleted category forever."""
    token = _stats_category.set(True)
    try:
        yield
    finally:
        _stats_category.reset(token)


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
        is_system = not _stats_category.get()
        for message in _sent_messages(result):
            if message.chat.type in _GROUP_TYPES:
                with contextlib.suppress(Exception):
                    await self._repo.log_bot_message(
                        message.chat.id, message.message_id, is_system=is_system
                    )
        return result


def _sent_messages(result: object) -> list[Message]:
    if isinstance(result, Message):
        return [result]
    if isinstance(result, list) and result and isinstance(result[0], Message):
        return result
    return []
