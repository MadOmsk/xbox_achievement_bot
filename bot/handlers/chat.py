"""Group commands. M2 covers /subscribe and /unsubscribe (SPEC 6.3)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject

from bot.db.repo import RecentAchievement, Repo, User
from bot.services.achievements import (
    platform_note,
    plural_achievements,
    rarity_badge,
)
from bot.services.stats import (
    counters_for,
    global_offset_minutes,
    month_start_utc,
)
from bot.util import humanize_ago, thousands

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


RECENT_DEFAULT = 5
RECENT_MAX = 20
TOP_LIMIT = 10


@router.message(Command("stats"))
async def stats(message: Message, repo: Repo, command: CommandObject) -> None:
    target = await _resolve(message, repo, command.args)
    if target is None:
        await message.answer(_UNKNOWN)
        return
    if not target.xuid:
        await message.answer("Этот человек ещё не подключил Xbox.")
        return

    settings_row = await repo.get_user_settings(target.tg_id)
    counters = await counters_for(
        repo, target.xuid, settings_row.tz_offset_min if settings_row else None
    )
    lines = [
        f"📊 {target.gamertag or 'без геймертега'}  ·  "
        f"gamerscore {thousands(target.gamerscore or 0)}",
        "",
        f"Сегодня:   {plural_achievements(counters.today)} (+{counters.today_score} G)",
        f"За месяц:  {plural_achievements(counters.month)} (+{thousands(counters.month_score)} G)",
        f"Всего:     {plural_achievements(counters.total)}",
    ]

    games = await repo.top_games(target.xuid)
    if games:
        lines += ["", "Больше всего очков:"]
        for place, game in enumerate(games, start=1):
            progress = (
                f" ({game.unlocked}/{game.total})"
                if game.unlocked is not None and game.total
                else ""
            )
            lines.append(
                f"{place}. {game.name or 'без названия'} — "
                f"{thousands(game.gamerscore or 0)} G{progress}"
            )
    await message.answer("\n".join(lines))


@router.message(Command("compare"))
async def compare(message: Message, repo: Repo, command: CommandObject) -> None:
    names = (command.args or "").split()
    if not names:
        await message.answer("Кого с кем? Например: /compare @user1 @user2")
        return

    first = await _resolve(message, repo, names[0])
    second = (
        await _resolve(message, repo, names[1])
        if len(names) > 1
        else (await repo.get_user(message.from_user.id) if message.from_user else None)
    )
    if first is None or second is None:
        await message.answer(_UNKNOWN)
        return
    if not first.xuid or not second.xuid:
        await message.answer("Кто-то из них ещё не подключил Xbox.")
        return

    left = await counters_for(repo, first.xuid, None)
    right = await counters_for(repo, second.xuid, None)
    lines = [
        f"⚔️ {first.gamertag or first.tg_id}  против  {second.gamertag or second.tg_id}",
        "",
        f"Гeймерскор:  {thousands(first.gamerscore or 0)}  ·  {thousands(second.gamerscore or 0)}",
        f"Сегодня:     {left.today}  ·  {right.today}",
        f"За месяц:    {left.month}  ·  {right.month}",
        f"Всего:       {thousands(left.total)}  ·  {thousands(right.total)}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("top"))
async def top(message: Message, repo: Repo) -> None:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Таблица лидеров считается по чату — набери её в группе.")
        return

    offset = await global_offset_minutes(repo)
    threshold = float(await repo.get_app_setting("rare_threshold_percent", "10") or 10)
    rows = await repo.chat_member_stats(message.chat.id, month_start_utc(offset), threshold)
    if not rows:
        await message.answer("За этот месяц пока никто ничего не выбил.")
        return

    lines = ["🏆 За месяц", ""]
    for place, row in enumerate(rows[:TOP_LIMIT], start=1):
        rare = f"  💎 {row.rare}" if row.rare else ""
        lines.append(
            f"{place}. {row.gamertag or row.tg_id}  {row.count}  +{thousands(row.score)} G{rare}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("recent"))
async def recent(message: Message, repo: Repo, command: CommandObject) -> None:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Лента считается по чату — набери команду в группе.")
        return

    limit = RECENT_DEFAULT
    if command.args and command.args.strip().isdigit():
        limit = max(1, min(int(command.args.strip()), RECENT_MAX))

    rows = await repo.chat_recent(message.chat.id, limit)
    if not rows:
        await message.answer("Пока пусто.")
        return
    await message.answer("🕘 Последние ачивки\n\n" + "\n".join(_recent_line(r) for r in rows))


def _recent_line(row: RecentAchievement) -> str:
    badge = rarity_badge(row.rarity_percent) or "·"
    return (
        f"{badge} {row.gamertag or 'кто-то'} — «{row.name}» · "
        f"{row.game or 'без названия'} · {row.gamerscore} G"
        f"{platform_note(row.platform, row.rarity_percent)} · "
        f"{humanize_ago(row.unlocked_at)}"
    )


_UNKNOWN = (
    "Не знаю такого. Bot API не умеет искать людей по @имени — "
    "я запоминаю тех, кто писал в чат. Можно ответить на сообщение человека "
    "командой /stats."
)


async def _resolve(message: Message, repo: Repo, argument: str | None) -> User | None:
    """Find who the command is about: reply, mention, @username or the sender."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return await repo.get_user(message.reply_to_message.from_user.id)

    for entity in message.entities or []:
        if entity.type == "text_mention" and entity.user:
            return await repo.get_user(entity.user.id)

    if argument:
        return await repo.find_user_by_username(argument.strip())

    return await repo.get_user(message.from_user.id) if message.from_user else None
