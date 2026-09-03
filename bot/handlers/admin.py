"""/admin — the operator's screen (SPEC 6.4). UI only: all data comes from services.

One message that redraws itself, like the user panel. Access is the
ADMIN_TG_IDS list from the config, checked on the router so that no single
handler can forget it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import Settings
from bot.db.repo import AdminUserRow, Repo
from bot.poller.fetcher import Fetcher
from bot.services.stats import counters_for, month_cutoff_utc, today_cutoff_utc
from bot.services.tables import truncate_name
from bot.util import humanize_ago, utcnow

log = logging.getLogger(__name__)

router = Router(name="admin")

PAGE_SIZE = 8
COMMON_ZONES = (
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Asia/Yekaterinburg",
    "Asia/Omsk",
    "Asia/Krasnoyarsk",
    "Asia/Irkutsk",
    "Asia/Vladivostok",
    "UTC",
)

STATUS_ICON = {"active": "✅", "invalid": "⚠️", "revoked": "🔕"}


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, settings: Settings) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and settings.is_admin(user.id)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def admin_command(message: Message, repo: Repo, fetcher: Fetcher) -> None:
    _awaiting_input.pop(message.from_user.id, None)  # a fresh /admin cancels any pending flow
    text, markup = await _home(repo, fetcher)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "a:home")
async def admin_home(callback: CallbackQuery, repo: Repo, fetcher: Fetcher) -> None:
    _awaiting_input.pop(callback.from_user.id, None)
    await _redraw(callback, *await _home(repo, fetcher))


# ------------------------------------------------------ free-text numeric settings

# Three settings share one "type a number, not a button" flow — a percent and
# two row caps are all free values that a handful of preset buttons could not
# cover anyway. Keyed by tg_id -> which setting, so a stray digit typed by an
# admin who isn't in this flow is never mistaken for input, and the one
# regex handler below knows which validation and label apply.
_awaiting_input: dict[int, str] = {}

RARE_THRESHOLD_MIN = 0.01
RARE_THRESHOLD_MAX = 100.0
LIMIT_MIN = 1
LIMIT_MAX = 50

_LIMIT_LABELS = {
    "summary_top_limit": "Строк в /summary",
    "stats_games_limit": "Игр в /stats",
    "hltb_recent_games_limit": "Игр-подсказок в /hltb",
}
_LIMIT_DEFAULTS = {
    "summary_top_limit": "15",
    "stats_games_limit": "15",
    "hltb_recent_games_limit": "20",
}


@router.callback_query(F.data == "a:rare")
async def rare_menu(callback: CallbackQuery, repo: Repo) -> None:
    current = await repo.get_app_setting("rare_threshold_percent", "10")
    _awaiting_input[callback.from_user.id] = "rare_threshold_percent"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:home"))
    await _redraw(
        callback,
        f"Порог «редкой» ачивки: {current}%\n\n"
        "Пришли новое значение одним числом, например 12 или 7.5 — от 0 до 100.\n\n"
        "Ниже этого процента ачивка считается редкой. Действует на всех, "
        "кто выбрал режим «только редкие»; на уже опубликованное не влияет.",
        builder.as_markup(),
    )


@router.callback_query(F.data == "a:limits")
async def limits_menu(callback: CallbackQuery, repo: Repo) -> None:
    builder = InlineKeyboardBuilder()
    for key, label in _LIMIT_LABELS.items():
        current = await repo.get_app_setting(key, _LIMIT_DEFAULTS[key])
        builder.row(
            InlineKeyboardButton(text=f"{label}: {current} ▸", callback_data=f"a:limit:{key}")
        )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:home"))
    await _redraw(
        callback,
        "Лимиты строк в таблицах.\n\n"
        "Когда реальных строк больше лимита, под таблицей появляется кнопка "
        "«Показать все» — по ней прилетает отдельное сообщение с полным списком.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("a:limit:"))
async def limit_menu(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    key = callback.data.rsplit(":", 1)[1]
    label = _LIMIT_LABELS[key]
    current = await repo.get_app_setting(key, _LIMIT_DEFAULTS[key])
    _awaiting_input[callback.from_user.id] = key
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:limits"))
    await _redraw(
        callback,
        f"{label}: {current}\n\nПришли новое значение целым числом, от {LIMIT_MIN} до {LIMIT_MAX}.",
        builder.as_markup(),
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(r"^\d+([.,]\d+)?$"))
async def numeric_setting_input(message: Message, repo: Repo, fetcher: Fetcher) -> None:
    assert message.from_user is not None and message.text is not None
    key = _awaiting_input.get(message.from_user.id)
    if key is None:
        return  # a plain number from an admin who isn't in this flow — ignore

    if key == "rare_threshold_percent":
        value = float(message.text.replace(",", "."))
        if not (RARE_THRESHOLD_MIN <= value <= RARE_THRESHOLD_MAX):
            await message.answer(
                f"Число должно быть от {RARE_THRESHOLD_MIN} до {RARE_THRESHOLD_MAX}. Ещё раз?"
            )
            return
        stored = f"{value:g}"
        confirm = f"Порог редкости: {stored}%"
    else:
        if "." in message.text or "," in message.text:
            await message.answer("Здесь только целое число. Ещё раз?")
            return
        value_int = int(message.text)
        if not (LIMIT_MIN <= value_int <= LIMIT_MAX):
            await message.answer(f"Число должно быть от {LIMIT_MIN} до {LIMIT_MAX}. Ещё раз?")
            return
        stored = str(value_int)
        confirm = f"{_LIMIT_LABELS[key]}: {stored}"

    del _awaiting_input[message.from_user.id]
    await repo.set_app_setting(key, stored, message.from_user.id)
    reply_text, markup = await _home(repo, fetcher)
    await message.answer(f"{confirm}\n\n{reply_text}", reply_markup=markup)


# ---------------------------------------------------------------- daily time


@router.callback_query(F.data == "a:time")
async def time_menu(callback: CallbackQuery, repo: Repo) -> None:
    current = await repo.get_app_setting("daily_summary_time", "23:00")
    builder = InlineKeyboardBuilder()
    for hour in range(24):
        label = f"{hour:02d}"
        mark = "• " if current.startswith(label) else ""
        builder.add(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"a:time:{hour}"))
    builder.adjust(6)
    builder.row(InlineKeyboardButton(text="Часовой пояс ▸", callback_data="a:tz"))
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:home"))
    await _redraw(
        callback,
        f"Время итога дня: {current}\n\nВо сколько отправлять сводку в чаты.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("a:time:"))
async def time_set(callback: CallbackQuery, repo: Repo, fetcher: Fetcher) -> None:
    assert callback.data is not None
    hour = int(callback.data.rsplit(":", 1)[1])
    await repo.set_app_setting("daily_summary_time", f"{hour:02d}:00", callback.from_user.id)
    await callback.answer(f"Итог дня в {hour:02d}:00")
    await _redraw(callback, *await _home(repo, fetcher))


@router.callback_query(F.data == "a:tz")
async def zone_menu(callback: CallbackQuery, repo: Repo) -> None:
    current = await repo.get_app_setting("timezone", "UTC")
    builder = InlineKeyboardBuilder()
    for zone in COMMON_ZONES:
        mark = "• " if zone == current else ""
        builder.add(InlineKeyboardButton(text=f"{mark}{zone}", callback_data=f"a:tzs:{zone}"))
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:time"))
    await _redraw(
        callback,
        f"Часовой пояс чата: {current}\n\n"
        "По нему считается итог дня. На личные счётчики не влияет — "
        "у каждого свой пояс.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("a:tzs:"))
async def zone_set(callback: CallbackQuery, repo: Repo, fetcher: Fetcher) -> None:
    assert callback.data is not None
    zone = callback.data.split(":", 2)[2]
    await repo.set_app_setting("timezone", zone, callback.from_user.id)
    await callback.answer(zone)
    await _redraw(callback, *await _home(repo, fetcher))


# --------------------------------------------------------------------- users


@router.callback_query(F.data.startswith("a:users:"))
async def users_page(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    page = int(callback.data.rsplit(":", 1)[1])
    await _redraw(callback, *await _users(repo, page))


@router.callback_query(F.data.startswith("a:u:"))
async def user_card(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    tg_id = int(callback.data.rsplit(":", 1)[1])
    await _redraw(callback, *await _card(repo, tg_id))


@router.callback_query(F.data.startswith("a:excl:"))
async def user_exclude(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    _, _, raw_id, raw_flag = callback.data.split(":")
    tg_id, excluded = int(raw_id), raw_flag == "1"
    await repo.set_excluded(tg_id, excluded, callback.from_user.id)
    await callback.answer("Исключён" if excluded else "Возвращён")
    await _redraw(callback, *await _card(repo, tg_id))


@router.callback_query(F.data.startswith("a:sync:"))
async def user_refresh(callback: CallbackQuery, repo: Repo, fetcher: Fetcher) -> None:
    """The only place in the whole interface that may call the API on demand
    (SPEC 1.5)."""
    assert callback.data is not None
    tg_id = int(callback.data.rsplit(":", 1)[1])
    user = await repo.get_user(tg_id)
    if user is None or not user.xuid:
        await callback.answer("Не подключён", show_alert=True)
        return

    await callback.answer("Обновляю…")
    try:
        summary = await fetcher.refresh_user(tg_id, user.xuid, user.gamertag or "Игрок")
    except Exception:
        log.exception("admin refresh of tg_id=%s failed", tg_id)
        await callback.answer("Не получилось обновить", show_alert=True)
        return
    text, markup = await _card(repo, tg_id)
    await _redraw(callback, f"{text}\n\n{summary}", markup)


# --------------------------------------------------------------------- chats


@router.callback_query(F.data == "a:chats")
async def chats_list(callback: CallbackQuery, repo: Repo) -> None:
    await _redraw(callback, *await _chats(repo))


@router.callback_query(F.data.startswith("a:chat:"))
async def chat_card(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    await _redraw(callback, *await _chat(repo, chat_id))


@router.callback_query(F.data.startswith("a:crar:"))
async def chat_rarity(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await repo.update_chat_settings(
        chat_id, rarity_mode="rare" if chat.rarity_mode == "all" else "all"
    )
    await callback.answer()
    await _redraw(callback, *await _chat(repo, chat_id))


@router.callback_query(F.data.startswith("a:cds:"))
async def chat_daily(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await repo.update_chat_settings(chat_id, daily_summary=0 if chat.daily_summary else 1)
    await callback.answer()
    await _redraw(callback, *await _chat(repo, chat_id))


@router.callback_query(F.data.startswith("a:coff:"))
async def chat_toggle_active(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await repo.set_chat_active(chat_id, not chat.is_active)
    await callback.answer("Отключён" if chat.is_active else "Включён")
    await _redraw(callback, *await _chat(repo, chat_id))


WIPE_WINDOW_HOURS = 24


@router.callback_query(F.data.startswith("a:cwipe:"))
async def chat_wipe_prompt(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    ids = await repo.bot_messages_since(chat_id, utcnow() - timedelta(hours=WIPE_WINDOW_HOURS))
    if not ids:
        await callback.answer("За последние 24 часа сообщений бота не нашёл.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, стереть", callback_data=f"a:cwipey:{chat_id}"))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data=f"a:chat:{chat_id}"))
    await _redraw(
        callback,
        f"Стереть {len(ids)} сообщений бота в «{chat.title or chat_id}» "
        f"за последние {WIPE_WINDOW_HOURS} часа?\n\n"
        "Необратимо. Считаются только сообщения, отправленные с тех пор, как завели "
        "этот учёт, — более старые бот не помнит.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("a:cwipey:"))
async def chat_wipe_confirm(callback: CallbackQuery, repo: Repo, bot: Bot) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    ids = await repo.bot_messages_since(chat_id, utcnow() - timedelta(hours=WIPE_WINDOW_HOURS))

    ok = True
    for start in range(0, len(ids), 100):  # Bot API caps deleteMessages at 100 ids per call
        try:
            await bot.delete_messages(chat_id, ids[start : start + 100])
        except Exception:
            log.info("bulk delete failed for chat %s, chunk at %s", chat_id, start)
            ok = False

    # Forgotten either way: Telegram silently skips ids it can no longer
    # delete (too old, already gone), and retrying those later would not
    # help either — nothing left worth keeping the log row for.
    await repo.forget_bot_messages(chat_id, ids)
    await callback.answer("Готово." if ok else "Частично — что-то не далось стереть.")
    await _redraw(callback, *await _chat(repo, chat_id))


# ------------------------------------------------------------------- screens


async def _home(repo: Repo, fetcher: Fetcher) -> tuple[str, InlineKeyboardMarkup]:
    users = await repo.admin_users()
    chats = await repo.admin_chats()
    threshold = await repo.get_app_setting("rare_threshold_percent", "10")
    summary_time = await repo.get_app_setting("daily_summary_time", "23:00")
    zone = await repo.get_app_setting("timezone", "UTC")

    active = sum(1 for u in users if u.token_status == "active" and not u.is_excluded)
    excluded = sum(1 for u in users if u.is_excluded)
    broken = sum(1 for u in users if u.token_status != "active")

    text = (
        "⚙️ Администрирование\n\n"
        f"Порог «редкой» ачивки:  {threshold}%\n"
        f"Итог дня:               {summary_time} ({zone})\n"
        f"Пользователей:          {len(users)} "
        f"({active} активных, {excluded} исключено, {broken} без входа)\n"
        f"Чатов:                  {sum(1 for c in chats if c.is_active)}\n"
        f"API (ачивки):           {_format_api_usage(fetcher.api_usage())}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Порог редкости ▸", callback_data="a:rare")],
            [InlineKeyboardButton(text="Время итога ▸", callback_data="a:time")],
            [InlineKeyboardButton(text="Лимиты таблиц ▸", callback_data="a:limits")],
            [InlineKeyboardButton(text="Пользователи ▸", callback_data="a:users:0")],
            [InlineKeyboardButton(text="Чаты ▸", callback_data="a:chats")],
        ]
    )
    return text, keyboard


async def _users(repo: Repo, page: int) -> tuple[str, InlineKeyboardMarkup]:
    users = await repo.admin_users()
    if not users:
        return "👥 Пока никто не подключился.", _back_home()

    today = await repo.achievement_counts_by_xuid(today_cutoff_utc())
    month = await repo.achievement_counts_by_xuid(month_cutoff_utc())

    pages = max(1, -(-len(users) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = users[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [f"👥 Пользователи  ({page + 1}/{pages})", ""]
    builder = InlineKeyboardBuilder()
    for user in chunk:
        name = user.gamertag or f"id{user.tg_id}"
        lines.append(
            f"{_icon(user)} {truncate_name(name, 14):<14} "
            f"{humanize_ago(user.last_online_at):<16} "
            f"{today.get(user.xuid, (0, 0))[0]} / {month.get(user.xuid, (0, 0))[0]}"
            f"{_note(user)}"
        )
        builder.row(
            InlineKeyboardButton(text=f"{_icon(user)} {name}", callback_data=f"a:u:{user.tg_id}")
        )

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="‹", callback_data=f"a:users:{page - 1}"))
    if page < pages - 1:
        navigation.append(InlineKeyboardButton(text="›", callback_data=f"a:users:{page + 1}"))
    if navigation:
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:home"))

    lines += ["", "Колонки: когда был в сети · ачивок сегодня / за месяц"]
    return "\n".join(lines), builder.as_markup()


async def _card(repo: Repo, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await repo.get_user(tg_id)
    if user is None or not user.xuid:
        return "Пользователь не найден.", _back_home()

    counters = await counters_for(repo, user.xuid)
    token = await repo.get_token(tg_id)
    presence = await repo.presence_of(user.xuid)
    chats = await repo.chats_of_user(tg_id)

    login = "— не подключён"
    if token is not None:
        login = {
            "active": f"✅ активен, обновлён {humanize_ago(token.last_refresh_at)}",
            "invalid": "⚠️ протух",
            "revoked": "🔕 отключён самим пользователем",
        }.get(token.status, token.status)

    online = "нет данных"
    if presence is not None:
        # Presence gives no name for PC titles, so fall back to the cache the
        # poller fills — an id in the card tells the admin nothing.
        game = presence.title_name or ""
        if not game and presence.title_id:
            game = await repo.title_name(presence.title_id) or presence.title_id
        game = game or "без игры"
        online = (
            f"{humanize_ago(presence.updated_at)}, {game}"
            if presence.state == "Online"
            else humanize_ago(presence.updated_at)
        )

    text = (
        f"👤 {user.gamertag or 'без геймертега'}  ·  gamerscore {user.gamerscore or 0}\n"
        f"XUID {user.xuid}\n\n"
        f"Вход:      {login}\n"
        f"В сети:    {online}\n"
        f"Подписан:  {', '.join(f'«{c}»' for c in chats) if chats else 'нигде'}\n"
        # No lifetime total here: seen_achievements is permanently
        # best-effort (SPEC 5.4), unlike these two date-bounded counters.
        f"Ачивок:    сегодня {counters.today} · за месяц {counters.month}"
    )
    if user.is_excluded:
        text += "\n\n🚫 Исключён из системы: не опрашивается и не публикуется."

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="↩️ Вернуть" if user.is_excluded else "🚫 Исключить из системы",
            callback_data=f"a:excl:{tg_id}:{0 if user.is_excluded else 1}",
        )
    )
    builder.row(InlineKeyboardButton(text="🔄 Обновить данные", callback_data=f"a:sync:{tg_id}"))
    builder.row(InlineKeyboardButton(text="‹ К списку", callback_data="a:users:0"))
    return text, builder.as_markup()


async def _chats(repo: Repo) -> tuple[str, InlineKeyboardMarkup]:
    chats = await repo.admin_chats()
    if not chats:
        return "Бот пока не добавлен ни в один чат.", _back_home()

    builder = InlineKeyboardBuilder()
    for chat in chats:
        mark = "" if chat.is_active else "⏸ "
        builder.row(
            InlineKeyboardButton(
                text=f"{mark}{chat.title or chat.chat_id} · {chat.subscribers}",
                callback_data=f"a:chat:{chat.chat_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:home"))
    return "💬 Чаты  (название · сколько человек публикуется)", builder.as_markup()


async def _chat(repo: Repo, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        return "Чат не найден.", _back_home()

    names = await repo.chat_subscriber_names(chat_id)
    text = (
        f"💬 {chat.title or chat_id}\n\n"
        f"Состояние:    {'активен' if chat.is_active else 'отключён'}\n"
        f"Публикуется:  {chat.subscribers} чел.\n"
        f"Редкость:     {'только редкие' if chat.rarity_mode == 'rare' else 'любые'}\n"
        f"Итог дня:     {'да' if chat.daily_summary else 'нет'}\n"
        f"Мин. G:       {chat.min_gamerscore}\n\n"
        + ("Подписаны: " + ", ".join(names) if names else "Подписанных пока нет.")
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"Редкость: {'только редкие' if chat.rarity_mode == 'rare' else 'любые'}",
            callback_data=f"a:crar:{chat_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"Итог дня: {'включён' if chat.daily_summary else 'выключен'}",
            callback_data=f"a:cds:{chat_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⏸ Отключить чат" if chat.is_active else "▶️ Включить чат",
            callback_data=f"a:coff:{chat_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🧹 Стереть сообщения бота (24ч)", callback_data=f"a:cwipe:{chat_id}"
        )
    )
    builder.row(InlineKeyboardButton(text="‹ К списку", callback_data="a:chats"))
    return text, builder.as_markup()


# ------------------------------------------------------------------- helpers


async def _find_chat(repo: Repo, chat_id: int):
    return next((c for c in await repo.admin_chats() if c.chat_id == chat_id), None)


def _back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data="a:home")]]
    )


def _format_api_usage(windows: list[tuple[int, int, float]]) -> str:
    """ "3/100 за 15с · 12/300 за 5 мин" — how close the shared achievements
    rate limiter is to Microsoft's own windows (SPEC 4), a diagnostic against
    a bug in the poller, not a persisted budget (removed once already, see
    приложение А — this reads the limiter's live in-memory counters, no
    database table, nothing to re-add)."""
    parts = []
    for used, limit, span in windows:
        label = f"{span / 60:g} мин" if span >= 60 else f"{span:g}с"
        parts.append(f"{used}/{limit} за {label}")
    return " · ".join(parts) if parts else "нет данных"


def _icon(user: AdminUserRow) -> str:
    if user.is_excluded:
        return "🚫"
    return STATUS_ICON.get(user.token_status or "", "—")


def _note(user: AdminUserRow) -> str:
    if user.is_excluded:
        return "  исключён"
    if user.token_status == "invalid":
        return "  вход протух"
    if user.token_status == "revoked":
        return "  отписался"
    return ""


async def _redraw(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """One message that redraws itself, not a new one per press (SPEC 6)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:
            # Telegram refuses an edit that changes nothing — harmless.
            pass
    await callback.answer()
