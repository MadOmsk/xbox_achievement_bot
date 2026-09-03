"""Logging every group message the bot sends, for the admin panel's
"стереть сообщения бота" (SPEC 6.4) — no official way to list a bot's own
past messages in Telegram, so this log is the only thing there is to
delete from."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram.types import Chat, Message

from bot.db.repo import Repo
from bot.services.message_log import MessageLogMiddleware, _sent_messages

CHAT_ID = -100777


def group_message(message_id: int = 1) -> Message:
    return Message(
        message_id=message_id, date=datetime.now(UTC), chat=Chat(id=CHAT_ID, type="supergroup")
    )


def private_message(message_id: int = 1) -> Message:
    return Message(message_id=message_id, date=datetime.now(UTC), chat=Chat(id=42, type="private"))


def test_sent_messages_unwraps_a_single_message() -> None:
    msg = group_message()
    assert _sent_messages(msg) == [msg]


def test_sent_messages_unwraps_a_media_group() -> None:
    msgs = [group_message(1), group_message(2)]
    assert _sent_messages(msgs) == msgs


def test_sent_messages_ignores_non_message_results() -> None:
    assert _sent_messages(True) == []
    assert _sent_messages(None) == []
    assert _sent_messages([]) == []


async def test_middleware_logs_only_group_messages(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    middleware = MessageLogMiddleware(repo)

    async def returns_group(_bot: object, _method: object) -> Message:
        return group_message(5)

    async def returns_private(_bot: object, _method: object) -> Message:
        return private_message(6)

    await middleware(returns_group, object(), object())  # type: ignore[arg-type]
    await middleware(returns_private, object(), object())  # type: ignore[arg-type]

    logged = await repo.bot_messages_since(CHAT_ID, datetime.now(UTC) - timedelta(minutes=1))
    assert logged == [5]


async def test_bot_messages_round_trip_and_forget(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    now = datetime.now(UTC)
    await repo.log_bot_message(CHAT_ID, 1)
    await repo.log_bot_message(CHAT_ID, 2)

    assert sorted(await repo.bot_messages_since(CHAT_ID, now - timedelta(minutes=1))) == [1, 2]
    # A cutoff in the future sees nothing — sanity check the "since" bound works.
    assert await repo.bot_messages_since(CHAT_ID, now + timedelta(minutes=1)) == []

    await repo.forget_bot_messages(CHAT_ID, [1])
    assert await repo.bot_messages_since(CHAT_ID, now - timedelta(minutes=1)) == [2]
