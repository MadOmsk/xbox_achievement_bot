"""Group commands (SPEC 6.3). UI only — no SQL outside repo, no API calls."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import (
    IS_MEMBER,
    IS_NOT_MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
)
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.repo import ChatPresenceRow, RecentAchievement, Repo, User
from bot.poller.daily import build_summary, current_rare_threshold
from bot.services.achievements import (
    platform_note,
    plural_achievements,
    rarity_badge,
)
from bot.services.stats import counters_for, global_offset_minutes, local_now
from bot.services.tables import render_table, total_line
from bot.util import cooldown_minutes_left, humanize_ago, thousands, utcnow

log = logging.getLogger(__name__)

router = Router(name="chat")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

# Per chat, not per person: rate-limiting only the requester would still let a
# whole chat spam it in turns.
SUMMARY_COOLDOWN_SECONDS = 600
_last_summary: dict[int, float] = {}

RECENT_GAMES_DAYS = 30
RECENT_DEFAULT = 5
RECENT_MAX = 20


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


# ------------------------------------------------------------- subscription


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
    """Same weight as /disconnect: losing your feed in a chat you might not
    remember subscribing in deserves a confirm, not an instant action."""
    if message.chat.type not in GROUP_TYPES or message.from_user is None:
        return
    if not await repo.is_subscribed(message.chat.id, message.from_user.id):
        await message.answer("Ты здесь и не публиковался.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отписаться",
                    callback_data=f"unsub:yes:{message.from_user.id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="unsub:no")],
        ]
    )
    await message.answer("Перестать публиковать твои ачивки в этом чате?", reply_markup=keyboard)


@router.callback_query(F.data == "unsub:no")
async def unsubscribe_cancel(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Отменил, всё остаётся как было.")
    await callback.answer()


@router.callback_query(F.data.startswith("unsub:yes:"))
async def unsubscribe_confirm(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    tg_id = int(callback.data.rsplit(":", 1)[1])
    # The confirm buttons are visible to the whole group, not just the person
    # who ran /unsubscribe — without this check anyone could confirm or
    # cancel someone else's unsubscribe.
    if callback.from_user.id != tg_id:
        await callback.answer("Это не твоя кнопка.", show_alert=True)
        return
    if not isinstance(callback.message, Message):
        return
    await repo.unsubscribe(callback.message.chat.id, tg_id)
    await callback.message.edit_text("Больше не публикую твои ачивки в этом чате.")
    await callback.answer()


# -------------------------------------------------------------------- stats


async def _build_stats_text(repo: Repo, target: User) -> str | None:
    """Shared by /stats and the clickable names in /online (SPEC 6.3) — one
    implementation, so a player's card looks the same no matter how it was
    opened."""
    if not target.xuid:
        return None

    settings_row = await repo.get_user_settings(target.tg_id)
    counters = await counters_for(
        repo, target.xuid, settings_row.tz_offset_min if settings_row else None
    )
    lines = [
        f"📊 <b>{target.gamertag or 'без геймертега'}</b>  ·  "
        f"gamerscore {thousands(target.gamerscore or 0)}",
        "",
        f"Сегодня:   {plural_achievements(counters.today)} (+{counters.today_score} G)",
        f"За месяц:  {plural_achievements(counters.month)} (+{thousands(counters.month_score)} G)",
        total_line("Всего", plural_achievements(counters.total)),
    ]

    games = await repo.recent_games(target.xuid, utcnow() - timedelta(days=RECENT_GAMES_DAYS))
    if games:
        lines += [
            "",
            f"<b>Игры за {RECENT_GAMES_DAYS} дней</b>",
            "",
            render_table(
                ["#", "Игра", "Ач.", "+G"],
                [
                    [
                        str(place),
                        game.name or "без названия",
                        str(game.unlocked or 0),
                        f"+{thousands(game.gamerscore or 0)}",
                    ]
                    for place, game in enumerate(games, start=1)
                ],
                ["<", "<", ">", ">"],
            ),
        ]
    return "\n".join(lines)


@router.message(Command("stats"))
async def stats(message: Message, repo: Repo, command: CommandObject) -> None:
    target = await _resolve(message, repo, command.args)
    if target is None:
        await message.answer(_UNKNOWN)
        return
    text = await _build_stats_text(repo, target)
    if text is None:
        await message.answer("Этот человек ещё не подключил Xbox.")
        return
    await message.answer(text, parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------- online


def _presence_text(row: ChatPresenceRow) -> str:
    if row.state == "Online" and row.title_id:
        return f"играет — {row.title_name or row.title_id}"
    if row.state == "Online":
        return "в сети, не играет"
    if row.state is not None:
        return "не в сети"
    return "нет данных"


def _presence_icon(row: ChatPresenceRow) -> str:
    if row.state == "Online" and row.title_id:
        return "🟢"
    if row.state == "Online":
        return "🟡"
    return "⚪"


@router.message(Command("online"))
async def online(message: Message, repo: Repo) -> None:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Список игроков — по чату, набери команду в группе.")
        return

    rows = await repo.chat_member_presence(message.chat.id)
    if not rows:
        await message.answer("В этом чате пока никто не подписан — /subscribe.")
        return

    lines = ["🎮 <b>Кто сейчас в игре</b>", ""]
    builder = InlineKeyboardBuilder()
    for row in rows:
        name = row.gamertag or f"id{row.tg_id}"
        lines.append(f"{_presence_icon(row)} {name} — {_presence_text(row)}")
        builder.button(text=name, callback_data=f"online:stats:{row.tg_id}")
    builder.adjust(3)
    await message.answer(
        "\n".join(lines), reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
    )


@router.callback_query(F.data.startswith("online:stats:"))
async def online_stats_button(callback: CallbackQuery, repo: Repo) -> None:
    """A tap on a name in /online opens that person's /stats — the whole
    point of listing names is to be able to look one up (SPEC 6.3)."""
    assert callback.data is not None
    tg_id = int(callback.data.rsplit(":", 1)[1])
    target = await repo.get_user(tg_id)
    if target is None:
        await callback.answer("Не нашёл такого пользователя.", show_alert=True)
        return
    text = await _build_stats_text(repo, target)
    await callback.answer()
    if text is not None and isinstance(callback.message, Message):
        await callback.message.answer(text, parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------- summary


async def _summary_or_cooldown(repo: Repo, chat_id: int) -> tuple[str | None, int]:
    """The report itself, or how many minutes are left before it can be asked
    for again (SPEC 5.7, 6.3) — one cooldown clock and one set of numbers
    no matter how it was triggered.

    Rate-limited per chat rather than per person: limiting only the requester
    would still let the whole chat spam it by taking turns.
    """
    minutes_left = cooldown_minutes_left(
        _last_summary.get(chat_id), time.monotonic(), SUMMARY_COOLDOWN_SECONDS
    )
    if minutes_left:
        return None, minutes_left

    # The cooldown covers "nothing new" too — that answer is still a message,
    # and without this it could be spammed just as freely as a real summary.
    _last_summary[chat_id] = time.monotonic()
    offset = await global_offset_minutes(repo)
    threshold = await current_rare_threshold(repo)
    text = await build_summary(repo, chat_id, threshold, local_now(offset).date())
    return text, 0


@router.message(Command("summary"))
async def summary_command(message: Message, repo: Repo) -> None:
    """The same report the scheduled job sends, on demand."""
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Сводка считается по чату — набери команду в группе.")
        return

    text, minutes_left = await _summary_or_cooldown(repo, message.chat.id)
    if minutes_left:
        await message.answer(f"Сводку уже присылали недавно. Ещё раз — через {minutes_left} мин.")
        return
    if text is None:
        await message.answer("За последние сутки в чате пока никто ничего не выбил.")
        return
    await message.answer(text, parse_mode=ParseMode.HTML)


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


# ---------------------------------------------------------------------- hub


# Most-used first, subscribe/unsubscribe at the end — those are one-time
# setup, not something read every time (SPEC 6.3).
HELP_TEXT = (
    "🎮 Я публикую сюда ачивки Xbox тех, кто подписался.\n\n"
    "Чтобы твои ачивки тоже летели сюда: сначала «Подключить Xbox», "
    "потом «Публиковать мои ачивки».\n\n"
    "Команды чата:\n"
    "/stats [@кто] — статистика игрока\n"
    "/online — кто сейчас в игре\n"
    "/recent [N] — последние ачивки чата\n"
    "/summary — сводка за сутки и за месяц\n\n"
    "/subscribe — публиковать мои ачивки здесь\n"
    "/unsubscribe — перестать\n\n"
    "Настройки — в личке: редкость, Xbox 360, часовой пояс."
)


def hub_keyboard(bot_username: str, chat_id: int) -> InlineKeyboardMarkup:
    """Three buttons, not a control panel: SPEC 6.3 walks through connect →
    publish in that order, so the keyboard should not offer more choices than
    that story needs.

    Buttons act on whoever presses them — that is why "Публиковать мои
    ачивки" is allowed here at all: SPEC 6.3 forbids rendering *someone
    else's* settings where any member could page through them, not a button
    that only ever touches the presser's own subscription.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Публиковать мои ачивки", callback_data="sub:on")],
            [
                InlineKeyboardButton(
                    text="🔗 Подключить Xbox",
                    # The chat id rides along in the deep-link payload so a
                    # successful login can auto-subscribe him right back here
                    # (SPEC 6.3) — see _parse_connect_payload in connect.py.
                    url=f"https://t.me/{bot_username}?start=connect{chat_id}",
                ),
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    url=f"https://t.me/{bot_username}?start=panel",
                ),
            ],
        ]
    )


async def hub_text(repo: Repo, chat_id: int) -> str:
    names = await repo.chat_subscriber_names(chat_id)
    if not names:
        return HELP_TEXT + "\n\nПока здесь никто не публикуется."
    return HELP_TEXT + "\n\nПубликуются: " + ", ".join(names)


@router.message(Command("help"))
async def help_command(message: Message, repo: Repo, bot: Bot) -> None:
    if message.chat.type not in GROUP_TYPES:
        await message.answer(HELP_TEXT)
        return
    me = await bot.me()
    await message.answer(
        await hub_text(repo, message.chat.id),
        reply_markup=hub_keyboard(me.username or "", message.chat.id),
    )


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def greet_new_chat(event: ChatMemberUpdated, repo: Repo, bot: Bot) -> None:
    """Say what to do the moment the bot lands in a group, not later."""
    if event.chat.type not in GROUP_TYPES:
        return
    await repo.upsert_chat(event.chat.id, event.chat.title, event.from_user.id)
    me = await bot.me()
    await bot.send_message(
        event.chat.id,
        await hub_text(repo, event.chat.id),
        reply_markup=hub_keyboard(me.username or "", event.chat.id),
    )


@router.callback_query(F.data == "sub:on")
async def subscribe_button(callback: CallbackQuery, repo: Repo, bot: Bot) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    user = await repo.get_user(callback.from_user.id)
    if user is None or not user.xuid:
        me = await bot.me()
        await callback.answer(f"Сначала подключи Xbox в личке: @{me.username}", show_alert=True)
        return

    await repo.upsert_chat(message.chat.id, message.chat.title, callback.from_user.id)
    if await repo.is_subscribed(message.chat.id, callback.from_user.id):
        await callback.answer("Ты уже публикуешься здесь.")
        return
    await repo.subscribe(message.chat.id, callback.from_user.id)
    await callback.answer("Готово, твои ачивки будут прилетать сюда.")
    await _refresh_hub(message, repo, bot)


async def _refresh_hub(message: Message, repo: Repo, bot: Bot) -> None:
    me = await bot.me()
    with contextlib.suppress(Exception):
        # Telegram refuses an edit that changes nothing — not an error.
        await message.edit_text(
            await hub_text(repo, message.chat.id),
            reply_markup=hub_keyboard(me.username or "", message.chat.id),
        )
