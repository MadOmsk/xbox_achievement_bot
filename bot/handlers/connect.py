"""/start, /connect_xbox, /disconnect_xbox and the timezone picker. UI only (CLAUDE.md)."""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import Settings
from bot.db.repo import Repo
from bot.handlers.keyboards import (
    TZ_MANUAL,
    TZ_MORE,
    TZ_SET,
    TZ_SKIP,
    connect_keyboard,
    format_offset,
    safe_edit,
    timezone_keyboard,
)
from bot.handlers.panel import render_panel
from bot.handlers.steam import prompt_for_link
from bot.services.connect import ConnectService
from bot.services.notify import AdminNotifier
from bot.util import parse_utc_offset

log = logging.getLogger(__name__)

router = Router(name="connect")

GREETING = (
    "Привет! Я публикую в чат достижения XBOX — свои и других участников.\n\n"
    "Что умею:\n"
    "• ловлю новые достижения и пишу о них в чат;\n"
    "• фильтрую по редкости, если не хочешь публиковать всё подряд;\n"
    "• веду личную статистику и итог дня.\n\n"
    "Начнём со входа через Microsoft."
)

TIMEZONE_PROMPT = "🕐 Твой часовой пояс?"

REVOKE_URL = "https://account.live.com/consent/Manage"


@router.message(CommandStart(deep_link=True))
async def start_with_payload(
    message: Message,
    command: CommandObject,
    repo: Repo,
    connect: ConnectService,
    settings: Settings,
    bot: Bot,
) -> None:
    """Deep link from a group chat: its buttons send people here (SPEC 6.3)."""
    await repo.ensure_user(message.chat.id, _username(message))
    if command.args == "panel":
        text, markup = await render_panel(repo, message.chat.id)
        await message.answer(text, reply_markup=markup)
        return
    if command.args == "connectsteam":
        # Same prompt-and-wait as every other door into this flow
        # (steam.py's prompt_for_link, 2026-09-05 follow-up) — a deep link
        # can't carry the profile URL itself, but landing here now arms the
        # wait too, so there's nothing left to type but the link itself.
        await prompt_for_link(bot, repo, settings, message.chat.id)
        return
    is_connect, origin_chat_id = _parse_connect_payload(command.args or "")
    if is_connect:
        # Straight to the login link: the person pressed «Подключить XBOX» in a
        # group and does not need the whole greeting again. If the button
        # carried which group it was pressed in, we auto-subscribe him there
        # once the login actually succeeds (see on_linked in bot/main.py).
        user = await repo.get_user(message.chat.id)
        if user is not None and user.xuid:
            await message.answer("XBOX уже подключён. Настройки — /panel.")
            return
        await _send_login_link(message, connect, origin_chat_id=origin_chat_id)
        return
    await _greet(message, repo, connect)


@router.message(CommandStart())
async def start(message: Message, repo: Repo, connect: ConnectService) -> None:
    await repo.ensure_user(message.chat.id, _username(message))
    await _greet(message, repo, connect)


@router.message(Command("connect_xbox"))
async def connect_command(message: Message, repo: Repo, connect: ConnectService) -> None:
    await repo.ensure_user(message.chat.id, _username(message))
    user = await repo.get_user(message.chat.id)
    if user is not None and user.xuid:
        await message.answer(
            "XBOX уже подключён. Если нужно войти заново — сначала /disconnect_xbox."
        )
        return
    await _send_login_link(message, connect)


@router.message(Command("disconnect_xbox"))
async def disconnect_command(message: Message, repo: Repo) -> None:
    user = await repo.get_user(message.chat.id)
    if user is None or not user.xuid:
        await message.answer("XBOX и так не подключён.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отключить", callback_data="disconnect:yes")],
            [InlineKeyboardButton(text="Отмена", callback_data="disconnect:no")],
        ]
    )
    await message.answer(
        "Отключить XBOX?\n\n"
        "Удалю токен и подписки. Историю достижений оставлю — она нужна статистике чата, "
        "и при повторном входе старые достижения не хлынут в чат заново.\n\n"
        f"Само разрешение остаётся в аккаунте Microsoft — убрать его можно только "
        f"самому: {REVOKE_URL}",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "disconnect:no")
async def disconnect_cancel(callback: CallbackQuery) -> None:
    # Nothing changed — just remove the prompt instead of leaving a
    # "cancelled" message behind for no reason.
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "disconnect:yes")
async def disconnect_confirm(callback: CallbackQuery, repo: Repo, notifier: AdminNotifier) -> None:
    tg_id = callback.from_user.id
    user = await repo.get_user(tg_id)
    gamertag = (user.gamertag if user else None) or f"id{tg_id}"
    if user is not None and user.xuid:
        await repo.delete_presence_state(user.xuid)
    await repo.delete_token(tg_id)
    await repo.delete_subscriptions_of_user(tg_id)
    await repo.unlink_xbox_account(tg_id)
    await notifier.user_disconnected(tg_id, gamertag, "сам через /disconnect_xbox")

    # Found while refactoring (2026-09-05): none of the edits in this file
    # tolerated a failed edit, unlike panel.py/steam.py's own — now they do.
    await safe_edit(
        callback,
        "Отключил. Вернуться можно в любой момент — /connect_xbox.\n\n"
        f"Разрешение в аккаунте Microsoft убирается тут: {REVOKE_URL}",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "relogin")
async def relogin(callback: CallbackQuery, connect: ConnectService) -> None:
    """Button from the "access expired" reminder (SPEC 5.1.1)."""
    url = connect.start_login(callback.from_user.id)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Войди заново — старые достижения в чат не полетят, они уже отмечены как виденные.",
            reply_markup=connect_keyboard(url),
        )
    await callback.answer()


@router.callback_query(F.data == "optout")
async def optout(callback: CallbackQuery, repo: Repo, notifier: AdminNotifier) -> None:
    """Left on purpose: subscriptions go, history stays, reminders stop."""
    tg_id = callback.from_user.id
    user = await repo.get_user(tg_id)
    await repo.set_token_status(tg_id, "revoked")
    await repo.delete_subscriptions_of_user(tg_id)
    await notifier.user_disconnected(
        tg_id, (user.gamertag if user else None) or f"id{tg_id}", "отписался кнопкой"
    )
    await safe_edit(
        callback,
        "Хорошо, больше не напоминаю. Историю достижений сохранил — "
        "вернуться можно в любой момент через /connect_xbox.",
    )
    await callback.answer()


@router.callback_query(F.data == TZ_MORE)
async def timezone_full_list(callback: CallbackQuery) -> None:
    await safe_edit(callback, TIMEZONE_PROMPT, timezone_keyboard(full=True))
    await callback.answer()


@router.callback_query(F.data == TZ_SKIP)
async def timezone_skip(callback: CallbackQuery) -> None:
    await safe_edit(callback, "Хорошо, пропустил. Поменять — в /panel.")
    await callback.answer()


@router.callback_query(F.data.startswith(f"{TZ_SET}:"))
async def timezone_set(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    minutes = int(callback.data.rsplit(":", 1)[1])
    await repo.ensure_user(callback.from_user.id, callback.from_user.username)
    await repo.update_user_settings(callback.from_user.id, tz_offset_min=minutes)
    _awaiting_manual_tz.pop(callback.from_user.id, None)

    await safe_edit(callback, f"Часовой пояс: {format_offset(minutes)}. Поменять можно в /panel.")
    await callback.answer()


# tg_id -> id of the prompt message to restore on a bad reply. Module-level
# and in-memory, same as admin.py's _awaiting_input — losing it on a restart
# just means asking again, nothing worth persisting to disk for.
_awaiting_manual_tz: dict[int, int] = {}

MANUAL_TZ_HINT = "Пришли смещение одним сообщением, со знаком: например +3, -5 или +5:30."


@router.callback_query(F.data == TZ_MANUAL)
async def timezone_manual_prompt(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        _awaiting_manual_tz[callback.from_user.id] = callback.message.message_id
    await safe_edit(callback, MANUAL_TZ_HINT)
    await callback.answer()


# A mandatory sign, unlike parse_utc_offset itself: admin.py's own numeric
# flow (rare threshold, row limits) accepts bare unsigned numbers in the same
# private chat, and a bare "3" must not be ambiguous between the two.
@router.message(
    F.chat.type == ChatType.PRIVATE, F.text.regexp(r"(?i)^(?:utc)?\s*[+-]\d{1,2}(?::[0-5]\d)?$")
)
async def timezone_manual_input(message: Message, repo: Repo) -> None:
    assert message.from_user is not None and message.text is not None
    prompt_id = _awaiting_manual_tz.pop(message.from_user.id, None)
    if prompt_id is None:
        return  # a stray signed number from someone not in this flow — ignore

    minutes = parse_utc_offset(message.text)
    if minutes is None:  # out of −12..+14 range — the regex alone can't catch that
        _awaiting_manual_tz[message.from_user.id] = prompt_id
        await message.answer(f"Это не похоже на реальный часовой пояс. {MANUAL_TZ_HINT}")
        return

    await repo.ensure_user(message.from_user.id, message.from_user.username)
    await repo.update_user_settings(message.from_user.id, tz_offset_min=minutes)
    await message.answer(f"Часовой пояс: {format_offset(minutes)}. Поменять можно в /panel.")


async def _greet(message: Message, repo: Repo, connect: ConnectService) -> None:
    user = await repo.get_user(message.chat.id)
    if user is not None and user.xuid:
        text, markup = await render_panel(repo, message.chat.id)
        await message.answer(text, reply_markup=markup)
        return
    await message.answer(GREETING)
    await _send_login_link(message, connect)


async def _send_login_link(
    message: Message, connect: ConnectService, *, origin_chat_id: int | None = None
) -> None:
    url = connect.start_login(message.chat.id, origin_chat_id=origin_chat_id)
    await message.answer(
        "Жми кнопку и войди своим аккаунтом Microsoft. Пароль вижу не я — "
        "его спрашивает сам Microsoft.",
        reply_markup=connect_keyboard(url),
    )


def _parse_connect_payload(args: str) -> tuple[bool, int | None]:
    """`?start=connect` from a private chat, or `?start=connect<chat_id>` from
    the group hub keyboard (SPEC 6.3) — (is it a connect payload, which
    group, if any)."""
    if args == "connect":
        return True, None
    if args.startswith("connect"):
        try:
            return True, int(args.removeprefix("connect"))
        except ValueError:
            return False, None
    return False, None


def _username(message: Message) -> str | None:
    return message.from_user.username if message.from_user else None
