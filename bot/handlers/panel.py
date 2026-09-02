"""/panel — the user's own screen. Reads the database only (SPEC 1.5)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.config import Settings
from bot.db.repo import Repo
from bot.handlers.keyboards import (
    DIGEST_NEVER,
    deep_link_keyboard,
    digest_keyboard,
    format_offset,
    panel_keyboard,
    rarity_keyboard,
    timezone_keyboard,
)
from bot.poller.fetcher import Fetcher
from bot.services.achievements import plural_achievements
from bot.services.stats import counters_for
from bot.util import cooldown_minutes_left, humanize_ago, parse_iso, thousands

log = logging.getLogger(__name__)

router = Router(name="panel")

GROUP_HINT_TTL = 30

# The one panel button that goes to the network (SPEC 5.8). Without a cooldown
# it is a way to hammer Xbox Live by holding a finger on the keyboard.
SYNC_COOLDOWN_SECONDS = 600
_last_sync: dict[int, float] = {}

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


@router.callback_query(F.data == "panel:sync")
async def panel_sync(
    callback: CallbackQuery, repo: Repo, fetcher: Fetcher, settings: Settings
) -> None:
    """Catch up on what was unlocked while the bot was down (SPEC 5.8)."""
    tg_id = callback.from_user.id
    user = await repo.get_user(tg_id)
    if user is None or not user.xuid:
        await callback.answer("Сначала подключи Xbox: /connect", show_alert=True)
        return

    minutes_left = cooldown_minutes_left(
        _last_sync.get(tg_id), time.monotonic(), SYNC_COOLDOWN_SECONDS
    )
    if minutes_left:
        await callback.answer(f"Уже синхронизировал. Ещё раз — через {minutes_left} мин.")
        return

    _last_sync[tg_id] = time.monotonic()
    await callback.answer("Синхронизирую…")

    target = next((t for t in await repo.pollable_users() if t.tg_id == tg_id), None)
    try:
        titles, published = await fetcher.catch_up(
            tg_id,
            user.xuid,
            user.gamertag or "Игрок",
            parse_iso(target.updated_at) if target else None,
            settings.catchup_publish_window_hours,
            settings.catchup_max_titles,
        )
    except Exception:
        log.exception("manual catch-up for tg_id=%s failed", tg_id)
        if isinstance(callback.message, Message):
            await callback.message.answer("Не получилось синхронизироваться, попробуй позже.")
        return

    summary = (
        f"Проверил игр: {titles}. Новых ачивок в чат: {published}."
        if titles
        else "Ничего нового — с последнего опроса ты никуда не заходил."
    )
    if isinstance(callback.message, Message):
        await callback.message.answer(summary)


@router.callback_query(F.data == "panel:rarity")
async def panel_rarity_menu(callback: CallbackQuery, repo: Repo) -> None:
    """Three modes for One/Series/PC, mirroring the show/hide switch below it
    for Xbox 360 — the threshold itself stays the admin's (SPEC 1.4)."""
    settings_row = await repo.get_user_settings(callback.from_user.id)
    current = settings_row.rarity_mode if settings_row else "all"
    threshold = await repo.get_app_setting("rare_threshold_percent", "10")
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(
                "One/Series/PC — что показывать в чате",
                reply_markup=rarity_keyboard(current, threshold or "10"),
            )
    await callback.answer()


@router.callback_query(F.data.startswith("panel:rarity:"))
async def panel_rarity_set(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    mode = callback.data.rsplit(":", 1)[1]
    await repo.update_user_settings(callback.from_user.id, rarity_mode=mode)
    await callback.answer()
    await _redraw(callback, repo)


@router.callback_query(F.data == "panel:x360")
async def panel_x360(callback: CallbackQuery, repo: Repo) -> None:
    settings_row = await repo.get_user_settings(callback.from_user.id)
    current = settings_row.show_x360 if settings_row else True
    await repo.update_user_settings(callback.from_user.id, show_x360=0 if current else 1)
    await _redraw(callback, repo)


@router.callback_query(F.data == "panel:digest")
async def panel_digest_menu(callback: CallbackQuery, repo: Repo) -> None:
    settings_row = await repo.get_user_settings(callback.from_user.id)
    current = settings_row.digest_threshold if settings_row else 3
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(
                "Сводка вместо отдельных сообщений\n\n"
                "Если за один раз в одной игре выбито столько ачивок или больше — "
                "в чат уйдёт одно сводное сообщение.",
                reply_markup=digest_keyboard(current),
            )
    await callback.answer()


@router.callback_query(F.data.startswith("panel:digest:"))
async def panel_digest_set(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    value = int(callback.data.rsplit(":", 1)[1])
    await repo.update_user_settings(callback.from_user.id, digest_threshold=value)
    await callback.answer("Никогда" if value >= DIGEST_NEVER else f"От {value}")
    await _redraw(callback, repo)


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
    settings_row = await repo.get_user_settings(tg_id)
    threshold = await repo.get_app_setting("rare_threshold_percent", "10")

    tz_offset = settings_row.tz_offset_min if settings_row else None
    keyboard = panel_keyboard(
        tz_offset,
        settings_row.rarity_mode if settings_row else "all",
        threshold or "10",
        settings_row.show_x360 if settings_row else True,
        settings_row.digest_threshold if settings_row else 3,
    )

    if user is None or not user.xuid:
        return (
            "👤 Панель\n\nВход: — не подключён\n\nПодключить: /connect",
            keyboard,
        )

    token = await repo.get_token(tg_id)
    login = LOGIN_STATUS.get(token.status, "— не подключён") if token else "— не подключён"
    counters = await counters_for(repo, user.xuid, tz_offset)
    last = await repo.last_achievement(user.xuid)

    lines = [
        f"👤 {user.gamertag or 'без геймертега'}  ·  gamerscore {thousands(user.gamerscore or 0)}",
        "",
        f"Вход:        {login}",
        f"Публикация:  {await _publication_status(repo, user.tg_id, user.is_excluded)}",
    ]
    if last is not None:
        game = last.title_name or "неизвестная игра"
        lines.append(f"Последняя:   «{last.name}» — {game}, {humanize_ago(last.unlocked_at)}")
    lines += [
        "",
        f"Сегодня:     {plural_achievements(counters.today)} (+{counters.today_score} G)",
        f"За месяц:    {plural_achievements(counters.month)} "
        f"(+{thousands(counters.month_score)} G)",
        f"Всего:       {plural_achievements(counters.total)}",
        "",
        f"Часовой пояс: {format_offset(tz_offset)}",
    ]
    if token is not None and token.status == "invalid":
        lines += ["", "Доступ к Xbox истёк — войди заново: /connect"]
    return "\n".join(lines), keyboard


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


async def _redraw(callback: CallbackQuery, repo: Repo) -> None:
    text, markup = await render_panel(repo, callback.from_user.id)
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            # Telegram rejects an edit that changes nothing; not an error.
            await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()
