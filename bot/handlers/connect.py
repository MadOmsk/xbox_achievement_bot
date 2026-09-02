"""/start, /connect, /disconnect and the timezone picker. UI only (CLAUDE.md)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db.repo import Repo
from bot.handlers.keyboards import (
    TZ_MORE,
    TZ_SET,
    TZ_SKIP,
    connect_keyboard,
    format_offset,
    timezone_keyboard,
)
from bot.handlers.panel import render_panel
from bot.services.connect import ConnectService
from bot.services.notify import AdminNotifier

log = logging.getLogger(__name__)

router = Router(name="connect")

GREETING = (
    "Привет! Я публикую в чат ачивки Xbox — свои и других участников.\n\n"
    "Что умею:\n"
    "• ловлю новые ачивки и пишу о них в чат;\n"
    "• фильтрую по редкости, если не хочешь публиковать всё подряд;\n"
    "• веду личную статистику и итог дня.\n\n"
    "Начнём со входа через Microsoft."
)

TIMEZONE_PROMPT = (
    "🕐 Твой часовой пояс?\n\n"
    "Нужен, чтобы «сегодня» и «за месяц» считались по твоему времени, "
    "а не по времени сервера."
)

REVOKE_URL = "https://account.live.com/consent/Manage"


@router.message(CommandStart(deep_link=True))
async def start_with_payload(
    message: Message, command: CommandObject, repo: Repo, connect: ConnectService
) -> None:
    """Deep link from a group chat: /panel there sends people here (SPEC 6.3)."""
    await repo.ensure_user(message.chat.id, _username(message))
    if command.args == "panel":
        text, markup = await render_panel(repo, message.chat.id)
        await message.answer(text, reply_markup=markup)
        return
    await _greet(message, repo, connect)


@router.message(CommandStart())
async def start(message: Message, repo: Repo, connect: ConnectService) -> None:
    await repo.ensure_user(message.chat.id, _username(message))
    await _greet(message, repo, connect)


@router.message(Command("connect"))
async def connect_command(message: Message, repo: Repo, connect: ConnectService) -> None:
    await repo.ensure_user(message.chat.id, _username(message))
    user = await repo.get_user(message.chat.id)
    if user is not None and user.xuid:
        await message.answer("Xbox уже подключён. Если нужно войти заново — сначала /disconnect.")
        return
    await _send_login_link(message, connect)


@router.message(Command("disconnect"))
async def disconnect_command(message: Message, repo: Repo) -> None:
    user = await repo.get_user(message.chat.id)
    if user is None or not user.xuid:
        await message.answer("Xbox и так не подключён.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отключить", callback_data="disconnect:yes")],
            [InlineKeyboardButton(text="Отмена", callback_data="disconnect:no")],
        ]
    )
    await message.answer(
        "Отключить Xbox?\n\n"
        "Удалю токен и подписки. Историю ачивок оставлю — она нужна статистике чата, "
        "и при повторном входе старые ачивки не хлынут в чат заново.\n\n"
        f"Само разрешение остаётся в аккаунте Microsoft — убрать его можно только "
        f"самому: {REVOKE_URL}",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "disconnect:no")
async def disconnect_cancel(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Отменил, всё остаётся как было.")
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
    await notifier.user_disconnected(tg_id, gamertag, "сам через /disconnect")

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Отключил. Вернуться можно в любой момент — /connect.\n\n"
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
            "Войди заново — старые ачивки в чат не полетят, они уже отмечены как виденные.",
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
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Хорошо, больше не напоминаю. Историю ачивок сохранил — "
            "вернуться можно в любой момент через /connect."
        )
    await callback.answer()


@router.callback_query(F.data == TZ_MORE)
async def timezone_full_list(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(TIMEZONE_PROMPT, reply_markup=timezone_keyboard(full=True))
    await callback.answer()


@router.callback_query(F.data == TZ_SKIP)
async def timezone_skip(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Хорошо, оставил общий часовой пояс. Поменять — в /panel.")
    await callback.answer()


@router.callback_query(F.data.startswith(f"{TZ_SET}:"))
async def timezone_set(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    minutes = int(callback.data.rsplit(":", 1)[1])
    await repo.ensure_user(callback.from_user.id, callback.from_user.username)
    await repo.update_user_settings(callback.from_user.id, tz_offset_min=minutes)

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Часовой пояс: {format_offset(minutes)}. Поменять можно в /panel."
        )
    await callback.answer()


async def _greet(message: Message, repo: Repo, connect: ConnectService) -> None:
    user = await repo.get_user(message.chat.id)
    if user is not None and user.xuid:
        text, markup = await render_panel(repo, message.chat.id)
        await message.answer(text, reply_markup=markup)
        return
    await message.answer(GREETING)
    await _send_login_link(message, connect)


async def _send_login_link(message: Message, connect: ConnectService) -> None:
    url = connect.start_login(message.chat.id)
    await message.answer(
        "Жми кнопку и войди своим аккаунтом Microsoft. Пароль вижу не я — "
        "его спрашивает сам Microsoft.",
        reply_markup=connect_keyboard(url),
    )


def _username(message: Message) -> str | None:
    return message.from_user.username if message.from_user else None
