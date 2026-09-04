"""/panel — the user's own screen. Reads the database only (SPEC 1.5)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import Settings
from bot.db.repo import Repo, UserChatRow
from bot.handlers.keyboards import (
    DIGEST_NEVER,
    deep_link_keyboard,
    digest_keyboard,
    disconnect_prompt_keyboard,
    format_offset,
    next_rarity_mode,
    panel_keyboard,
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
        await callback.answer("Сначала подключи Xbox: /connect_xbox", show_alert=True)
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
async def panel_rarity_cycle(callback: CallbackQuery, repo: Repo) -> None:
    """One mode for every connected platform (SPEC 9, M-Steam-2e; used to be
    paired with a separate Xbox 360 show/hide switch, folded into this one
    instead of growing a second per-platform toggle) — the threshold itself
    stays the admin's (SPEC 1.4). One tap advances to the next mode."""
    settings_row = await repo.get_user_settings(callback.from_user.id)
    current = settings_row.rarity_mode if settings_row else "all"
    mode = next_rarity_mode(current)
    await repo.update_user_settings(callback.from_user.id, rarity_mode=mode)
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


@router.callback_query(F.data == "panel:disconnect")
async def panel_disconnect_prompt(callback: CallbackQuery, repo: Repo) -> None:
    """Same confirmation as /disconnect_xbox — the actual disconnect handlers
    (disconnect:yes / disconnect:no in connect.py) just edit whatever message
    triggered them, so they work unchanged from the panel too."""
    from bot.handlers.connect import REVOKE_URL

    user = await repo.get_user(callback.from_user.id)
    if user is None or not user.xuid:
        await callback.answer("Xbox и так не подключён.", show_alert=True)
        return
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(
                "Отключить Xbox?\n\n"
                "Удалю токен и подписки. Историю ачивок оставлю — она нужна статистике "
                "чата, и при повторном входе старые ачивки не хлынут в чат заново.\n\n"
                f"Само разрешение остаётся в аккаунте Microsoft — убрать его можно "
                f"только самому: {REVOKE_URL}",
                reply_markup=disconnect_prompt_keyboard(from_panel=True),
                disable_web_page_preview=True,
            )
    await callback.answer()


@router.callback_query(F.data == "panel:disconnect:no")
async def panel_disconnect_cancel(callback: CallbackQuery, repo: Repo) -> None:
    # Cancelling here edits the panel message itself, so restore the panel
    # in place instead of leaving a throwaway "cancelled" message behind.
    text, markup = await render_panel(repo, callback.from_user.id)
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "panel:tz")
async def panel_timezone(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "🕐 Часовой пояс — по нему считаются «сегодня» и «за месяц».",
            reply_markup=timezone_keyboard(skippable=False),
        )
    await callback.answer()


# ------------------------------------------------------------------ my chats


@router.callback_query(F.data == "panel:chatlist")
async def panel_chat_list(callback: CallbackQuery, repo: Repo) -> None:
    await _redraw_chat_list(callback, repo)


async def _redraw_chat_list(callback: CallbackQuery, repo: Repo) -> None:
    chats = await repo.user_chats(callback.from_user.id)
    text = "💬 Мои чаты"
    if not chats:
        text += "\n\nПока ни в одном чате тебя не видел — ни подписок, ни сообщений."
    builder = InlineKeyboardBuilder()
    for chat in chats:
        mark = "✅" if chat.is_subscribed else "⚪"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {chat.title or chat.chat_id}",
                callback_data=f"panel:chat:{chat.chat_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="panel:refresh"))
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


async def _find_user_chat(repo: Repo, tg_id: int, chat_id: int) -> UserChatRow | None:
    return next((c for c in await repo.user_chats(tg_id) if c.chat_id == chat_id), None)


async def _chat_card(
    repo: Repo, tg_id: int, chat_id: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    chat = await _find_user_chat(repo, tg_id, chat_id)
    if chat is None:
        return None
    title = chat.title or chat.chat_id
    builder = InlineKeyboardBuilder()
    if chat.is_subscribed:
        text = f"💬 {title}\n\nПубликация: ✅ включена"
        builder.row(
            InlineKeyboardButton(text="Отписаться", callback_data=f"panel:chatunsub:{chat_id}")
        )
    else:
        text = f"💬 {title}\n\nПубликация: ⏸ выключена"
        builder.row(
            InlineKeyboardButton(text="Подписаться", callback_data=f"panel:chatsub:{chat_id}")
        )
        builder.row(
            InlineKeyboardButton(text="Удалить из списка", callback_data=f"panel:chatdel:{chat_id}")
        )
    builder.row(InlineKeyboardButton(text="‹ К списку чатов", callback_data="panel:chatlist"))
    return text, builder.as_markup()


async def _redraw_chat_card(callback: CallbackQuery, repo: Repo, chat_id: int) -> None:
    built = await _chat_card(repo, callback.from_user.id, chat_id)
    if built is None:
        await _redraw_chat_list(callback, repo)
        return
    text, markup = built
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("panel:chat:"))
async def panel_chat_card(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    await _redraw_chat_card(callback, repo, chat_id)


@router.callback_query(F.data.startswith("panel:chatsub:"))
async def panel_chat_subscribe(callback: CallbackQuery, repo: Repo) -> None:
    """No confirm — resubscribing has no downside, unlike unsubscribing or
    deleting (SPEC 6.2)."""
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    user = await repo.get_user(callback.from_user.id)
    if user is None or not user.xuid:
        await callback.answer("Сначала подключи Xbox: /connect_xbox", show_alert=True)
        return
    await repo.subscribe(chat_id, callback.from_user.id)
    await callback.answer("Подписал")
    await _redraw_chat_card(callback, repo, chat_id)


@router.callback_query(F.data.startswith("panel:chatunsub:"))
async def panel_chat_unsub_prompt(callback: CallbackQuery, repo: Repo) -> None:
    """Same weight as the standalone /unsubscribe — a confirm, not an instant
    action (SPEC 6.3): losing a feed in a chat deserves a second tap."""
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_user_chat(repo, callback.from_user.id, chat_id)
    if chat is None:
        await _redraw_chat_list(callback, repo)
        return
    title = chat.title or chat.chat_id
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да, отписаться", callback_data=f"panel:chatunsuby:{chat_id}")
    )
    builder.row(InlineKeyboardButton(text="Отмена", callback_data=f"panel:chat:{chat_id}"))
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(
                f"Перестать публиковать твои ачивки в «{title}»?", reply_markup=builder.as_markup()
            )
    await callback.answer()


@router.callback_query(F.data.startswith("panel:chatunsuby:"))
async def panel_chat_unsub_confirm(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    await repo.unsubscribe(chat_id, callback.from_user.id)
    await callback.answer("Отписал")
    await _redraw_chat_card(callback, repo, chat_id)


@router.callback_query(F.data.startswith("panel:chatdel:"))
async def panel_chat_delete_prompt(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_user_chat(repo, callback.from_user.id, chat_id)
    if chat is None:
        await _redraw_chat_list(callback, repo)
        return
    title = chat.title or chat.chat_id
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, удалить", callback_data=f"panel:chatdely:{chat_id}"))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data=f"panel:chat:{chat_id}"))
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(
                f"Убрать «{title}» из списка? Как будто ты там никогда не был — "
                "не бан, снова окажешься в списке, если подпишешься или напишешь туда.",
                reply_markup=builder.as_markup(),
            )
    await callback.answer()


@router.callback_query(F.data.startswith("panel:chatdely:"))
async def panel_chat_delete_confirm(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    await repo.forget_chat_membership(chat_id, callback.from_user.id)
    await callback.answer("Убрал")
    await _redraw_chat_list(callback, repo)


RECENT_IN_PANEL = 5


async def render_panel(repo: Repo, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await repo.get_user(tg_id)
    settings_row = await repo.get_user_settings(tg_id)
    connected = user is not None and bool(user.xuid)
    steam_link = await repo.get_platform_link(tg_id, "steam")

    token = await repo.get_token(tg_id) if connected else None
    needs_reconnect = token is not None and token.status == "invalid"
    tz_offset = settings_row.tz_offset_min if settings_row else None
    keyboard = panel_keyboard(
        tz_offset,
        settings_row.rarity_mode if settings_row else "all",
        settings_row.digest_threshold if settings_row else 3,
        connected=connected,
        needs_reconnect=needs_reconnect,
    )

    if user is None or not user.xuid:
        text = "👤 Панель\n\nВход Xbox: — не подключён"
        if steam_link is not None:
            text += f"\nВход Steam: {steam_link.display_name}"
        return text, keyboard

    login = LOGIN_STATUS.get(token.status, "— не подключён") if token else "— не подключён"
    counters = await counters_for(repo, tg_id)
    playing = await _now_playing(repo, user.xuid)
    recent = await repo.recent_achievements(user.xuid, RECENT_IN_PANEL)

    lines = [
        f"👤 {user.gamertag or 'без геймертега'}  ·  gamerscore {thousands(user.gamerscore or 0)}",
        "",
        f"Вход Xbox:   {login}",
    ]
    # "Сегодня"/"За месяц" below already sum Steam achievements in too
    # (SPEC 9, M-Steam-2e) — this line is just the persona name, no counter
    # of its own next to it, same as /stats' per-platform lines.
    if steam_link is not None:
        lines.append(f"Вход Steam:  {steam_link.display_name}")
    lines += [
        f"Публикация:  {await _publication_status(repo, user.tg_id, user.is_excluded)}",
        f"Сейчас:      {playing}",
        "",
        f"Сегодня:     {plural_achievements(counters.today)} (+{counters.today_score} G)",
        f"За месяц:    {plural_achievements(counters.month)} "
        f"(+{thousands(counters.month_score)} G)",
        # No lifetime "Всего": seen_achievements is permanently best-effort
        # (SPEC 5.4), unlike the two date-bounded counters above it.
        f"Часовой пояс: {format_offset(tz_offset)}",
    ]
    if needs_reconnect:
        lines += ["", "Доступ к Xbox истёк — жми «Подключить заново» ниже."]
    if recent:
        lines += ["", "Последние ачивки:"]
        lines += [
            f"🏆 «{item.name}» — {item.title_name or 'неизвестная игра'}, "
            f"{humanize_ago(item.unlocked_at)}"
            for item in recent
        ]
    return "\n".join(lines), keyboard


async def _now_playing(repo: Repo, xuid: str) -> str:
    presence = await repo.presence_of(xuid)
    if presence is None:
        return "нет данных"
    if presence.state != "Online":
        return f"не в сети ({humanize_ago(presence.updated_at)})"
    if not presence.title_id:
        return "в сети, не играет"
    # Presence gives no name for PC titles — fall back to the cache the
    # poller fills (SPEC 4), same as the admin card.
    game = presence.title_name or await repo.title_name(presence.title_id) or presence.title_id
    return f"играет — {game}"


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
