"""`/connect_steam`, `/disconnect_steam` (M-Steam-1, TODO.md; backfill —
SPEC 9, M-Steam-2d).

Private chat only, like /connect_xbox and /panel — this is personal, not a group
setting (SPEC 6.3's "настройки в общий чат не попадают никогда" applies the
same way here). Found live: an earlier version filtered group chat out at
the router level with no reply at all — from inside a group that reads as
the bot simply not responding, not as "try this in DM". Redirects instead,
same pattern as /panel in a group (panel.py's panel_in_group).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import Settings
from bot.db.repo import Repo
from bot.handlers.keyboards import deep_link_keyboard
from bot.poller.steam_fetcher import SteamFetcher
from bot.services.steam.client import SteamApiError, get_profile, resolve_steam_id

log = logging.getLogger(__name__)

router = Router(name="steam")

NOT_CONFIGURED = "Подключение Steam пока не настроено — обратитесь к администратору."
PRIVACY_URL = "https://steamcommunity.com/my/edit/settings"
GROUP_HINT_TTL = 30
# Shared with connect.py's ?start=connectsteam deep link (the hub keyboard's
# "Подключить Steam" button) — same prompt either way someone gets here.
LINK_PROMPT = (
    "Пришли ссылку на свой профиль Steam (steamcommunity.com/id/...) "
    "или SteamID64 — команда так: /connect_steam <ссылка>."
)


async def _redirect_to_dm(message: Message, bot: Bot, hint_text: str) -> None:
    # No deep-link payload: /connect_steam needs an argument (the profile
    # link) a button tap can't supply anyway, so this just opens the DM —
    # the hint text says what to type once there, same honesty as the
    # command's own "empty args" reply.
    me = await bot.me()
    hint = await message.answer(
        hint_text, reply_markup=deep_link_keyboard(f"https://t.me/{me.username}")
    )
    asyncio.create_task(_delete_later(bot, hint.chat.id, hint.message_id))  # noqa: RUF006


async def _delete_later(bot: Bot, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(GROUP_HINT_TTL)
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id, message_id)


@router.message(Command("connect_steam"), F.chat.type != ChatType.PRIVATE)
async def connect_steam_in_group(message: Message, bot: Bot) -> None:
    await _redirect_to_dm(message, bot, "Напиши мне в личку: /connect_steam <ссылка на профиль>.")


@router.message(Command("disconnect_steam"), F.chat.type != ChatType.PRIVATE)
async def disconnect_steam_in_group(message: Message, bot: Bot) -> None:
    await _redirect_to_dm(message, bot, "Эта команда — в личке.")


@router.message(Command("connect_steam"), F.chat.type == ChatType.PRIVATE)
async def connect_steam(
    message: Message,
    repo: Repo,
    settings: Settings,
    command: CommandObject,
    steam_fetcher: SteamFetcher,
    bot: Bot,
) -> None:
    if settings.steam_api_key is None:
        await message.answer(NOT_CONFIGURED)
        return

    raw = (command.args or "").strip()
    if not raw:
        await message.answer(LINK_PROMPT)
        return

    api_key = settings.steam_api_key.get_secret_value()
    try:
        steam_id = await resolve_steam_id(api_key, raw)
        profile = await get_profile(api_key, steam_id)
    except SteamApiError:
        await message.answer(
            "Не нашёл такой профиль Steam. Проверь ссылку — например, "
            "https://steamcommunity.com/id/gaben."
        )
        return

    if not profile.is_public:
        await message.answer(
            "Профиль есть, но игровая статистика скрыта — я не смогу читать "
            "ачивки. Сделай её публичной и попробуй снова: "
            f"{PRIVACY_URL} → «Игровая статистика» → «Всем»."
        )
        return

    username = message.from_user.username if message.from_user else None
    await repo.ensure_user(message.chat.id, username)
    await repo.link_platform_account(
        message.chat.id, "steam", profile.steam_id, profile.persona_name
    )
    await message.answer(f"Подключил Steam: {profile.persona_name}.")

    # Backgrounded (SPEC 9, M-Steam-2d) — a big library is genuinely
    # hundreds of requests, the /connect_steam reply must not wait for it.
    # Run on every link, not just the first (link_platform_account already
    # replaces an existing one) — idempotent and safe, same reasoning as
    # Xbox's refresh_after_reconnect (main.py).
    await message.answer("Читаю твою историю ачивок Steam, это может занять пару минут…")
    asyncio.create_task(  # noqa: RUF006
        _backfill_and_notify(bot, steam_fetcher, message.chat.id, profile.steam_id)
    )


async def _backfill_and_notify(
    bot: Bot, fetcher: SteamFetcher, tg_id: int, steam_id: str
) -> None:
    try:
        count = await fetcher.backfill(tg_id, steam_id)
    except Exception:
        log.exception("steam backfill for tg_id=%s failed", tg_id)
        await bot.send_message(
            tg_id,
            "Не смог перечитать твою историю ачивок Steam. Публикация пока "
            "выключена — привяжи аккаунт заново чуть позже: /connect_steam.",
        )
        return
    await bot.send_message(
        tg_id, f"Готово: перечитал {count} уже выбитых ачивок Steam — в чат они не полетят."
    )


@router.message(Command("disconnect_steam"), F.chat.type == ChatType.PRIVATE)
async def disconnect_steam_command(message: Message, repo: Repo) -> None:
    link = await repo.get_platform_link(message.chat.id, "steam")
    if link is None:
        await message.answer("Steam и так не подключён.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отключить", callback_data="steam:disconnect:yes")],
            [InlineKeyboardButton(text="Отмена", callback_data="steam:disconnect:no")],
        ]
    )
    await message.answer(f"Отключить Steam ({link.display_name})?", reply_markup=keyboard)


@router.callback_query(F.data == "steam:disconnect:no")
async def disconnect_steam_cancel(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "steam:disconnect:yes")
async def disconnect_steam_confirm(callback: CallbackQuery, repo: Repo) -> None:
    link = await repo.get_platform_link(callback.from_user.id, "steam")
    await repo.unlink_platform_account(callback.from_user.id, "steam")
    if link is not None:
        # Symmetric with Xbox's disconnect (connect.py, delete_presence_state)
        # — a stale presence row would otherwise keep answering /online for
        # an account that's no longer linked to anyone.
        await repo.delete_steam_presence_state(link.external_id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Отключил Steam. Вернуться можно в любой момент.")
    await callback.answer()
