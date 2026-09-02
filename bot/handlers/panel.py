"""/panel — the user's own screen. Reads the database only (SPEC 1.5)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.db.repo import Repo
from bot.handlers.keyboards import (
    deep_link_keyboard,
    format_offset,
    panel_keyboard,
    timezone_keyboard,
)

log = logging.getLogger(__name__)

router = Router(name="panel")

GROUP_HINT_TTL = 30

LOGIN_STATUS = {
    "active": "✅ активен",
    "invalid": "⚠️ требуется повторный вход",
    "revoked": "— отключён",
}


@router.message(Command("panel"), F.chat.type == ChatType.PRIVATE)
async def panel_command(message: Message, repo: Repo) -> None:
    await repo.ensure_user(message.chat.id, _username(message))
    text, markup = await render_panel(repo, message.chat.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("panel"))
async def panel_in_group(message: Message, bot: Bot) -> None:
    """Settings never render in a group: an inline keyboard there is clickable
    by everyone in the chat (SPEC 6.3)."""
    me = await bot.me()
    hint = await message.answer(
        "Настройки — в личке.",
        reply_markup=deep_link_keyboard(f"https://t.me/{me.username}?start=panel"),
    )
    asyncio.create_task(_delete_later(bot, hint.chat.id, hint.message_id))  # noqa: RUF006


@router.callback_query(F.data == "panel:refresh")
async def panel_refresh(callback: CallbackQuery, repo: Repo) -> None:
    text, markup = await render_panel(repo, callback.from_user.id)
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            # Telegram rejects an edit that changes nothing; that is not an error.
            await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Обновил")


@router.callback_query(F.data == "panel:tz")
async def panel_timezone(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "🕐 Часовой пояс — по нему считаются «сегодня» и «за месяц».",
            reply_markup=timezone_keyboard(skippable=False),
        )
    await callback.answer()


async def render_panel(repo: Repo, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await repo.get_user(tg_id)
    settings = await repo.get_user_settings(tg_id)
    tz_offset = settings.tz_offset_min if settings else None

    if user is None or not user.xuid:
        return (
            "👤 Панель\n\nВход: — не подключён\n\nПодключить: /connect",
            panel_keyboard(tz_offset),
        )

    token = await repo.get_token(tg_id)
    login = LOGIN_STATUS.get(token.status, "— не подключён") if token else "— не подключён"

    lines = [
        f"👤 {user.gamertag or 'без геймертега'}",
        "",
        f"Вход:        {login}",
        f"Публикация:  {await _publication_status(repo, user.tg_id, user.is_excluded)}",
        f"Часовой пояс: {format_offset(tz_offset)}",
    ]
    if token is not None and token.status == "invalid":
        lines += ["", "Доступ к Xbox истёк — войди заново: /connect"]
    return "\n".join(lines), panel_keyboard(tz_offset)


async def _publication_status(repo: Repo, tg_id: int, is_excluded: bool) -> str:
    if is_excluded:
        # An exclusion is never silent: the person sees it here (SPEC 6.4).
        return "🚫 исключён администратором"
    chats = await repo.chats_of_user(tg_id)
    if not chats:
        return "— не подписан ни в одном чате"
    return "✅ в " + ", ".join(f"«{title}»" for title in chats)


async def _delete_later(bot: Bot, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(GROUP_HINT_TTL)
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id, message_id)


def _username(message: Message) -> str | None:
    return message.from_user.username if message.from_user else None
