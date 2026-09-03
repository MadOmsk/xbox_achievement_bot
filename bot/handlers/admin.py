"""/admin — the operator's screen (SPEC 6.4). UI only: all data comes from services.

One message that redraws itself, like the user panel. Access is the
ADMIN_TG_IDS list from the config, checked on the router so that no single
handler can forget it.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
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
from bot.services.stats import (
    counters_for,
    global_offset_minutes,
    month_start_utc,
    today_cutoff_utc,
)
from bot.util import humanize_ago

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
    _awaiting_rare_input.discard(message.from_user.id)  # a fresh /admin cancels any pending flow
    text, markup = await _home(repo, fetcher)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "a:home")
async def admin_home(callback: CallbackQuery, repo: Repo, fetcher: Fetcher) -> None:
    _awaiting_rare_input.discard(callback.from_user.id)
    await _redraw(callback, *await _home(repo, fetcher))


# ------------------------------------------------------------ rare threshold

# Waiting for a typed number, not a button — a percent is naturally a free
# value, and eight preset buttons could not cover it anyway. Keyed by tg_id so
# a stray digit typed by an admin who isn't in this flow is never mistaken
# for a threshold.
_awaiting_rare_input: set[int] = set()

RARE_THRESHOLD_MIN = 0.01
RARE_THRESHOLD_MAX = 100.0


@router.callback_query(F.data == "a:rare")
async def rare_menu(callback: CallbackQuery, repo: Repo) -> None:
    current = await repo.get_app_setting("rare_threshold_percent", "10")
    _awaiting_rare_input.add(callback.from_user.id)
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


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(r"^\d+([.,]\d+)?$"))
async def rare_threshold_input(message: Message, repo: Repo, fetcher: Fetcher) -> None:
    assert message.from_user is not None and message.text is not None
    if message.from_user.id not in _awaiting_rare_input:
        return  # a plain number from an admin who isn't in this flow — ignore

    value = float(message.text.replace(",", "."))
    if not (RARE_THRESHOLD_MIN <= value <= RARE_THRESHOLD_MAX):
        await message.answer(
            f"Число должно быть от {RARE_THRESHOLD_MIN} до {RARE_THRESHOLD_MAX}. Ещё раз?"
        )
        return

    _awaiting_rare_input.discard(message.from_user.id)
    text = f"{value:g}"
    await repo.set_app_setting("rare_threshold_percent", text, message.from_user.id)
    reply_text, markup = await _home(repo, fetcher)
    await message.answer(f"Порог редкости: {text}%\n\n{reply_text}", reply_markup=markup)


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
            [InlineKeyboardButton(text="Пользователи ▸", callback_data="a:users:0")],
            [InlineKeyboardButton(text="Чаты ▸", callback_data="a:chats")],
        ]
    )
    return text, keyboard


async def _users(repo: Repo, page: int) -> tuple[str, InlineKeyboardMarkup]:
    users = await repo.admin_users()
    if not users:
        return "👥 Пока никто не подключился.", _back_home()

    offset = await global_offset_minutes(repo)
    today = await repo.achievement_counts_by_xuid(today_cutoff_utc())
    month = await repo.achievement_counts_by_xuid(month_start_utc(offset))

    pages = max(1, -(-len(users) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = users[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [f"👥 Пользователи  ({page + 1}/{pages})", ""]
    builder = InlineKeyboardBuilder()
    for user in chunk:
        name = user.gamertag or f"id{user.tg_id}"
        lines.append(
            f"{_icon(user)} {name:<14} {humanize_ago(user.last_online_at):<16} "
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

    settings_row = await repo.get_user_settings(tg_id)
    counters = await counters_for(
        repo, user.xuid, settings_row.tz_offset_min if settings_row else None
    )
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
        f"Ачивок:    сегодня {counters.today} · за месяц {counters.month} · "
        f"всего {counters.total}"
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
