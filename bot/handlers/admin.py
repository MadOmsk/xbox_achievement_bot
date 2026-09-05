"""/admin — the operator's screen (SPEC 6.4). UI only: all data comes from services.

One message that redraws itself, like the user panel. Access is the
ADMIN_TG_IDS list from the config, checked on the router so that no single
handler can forget it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
from bot.db.repo import AdminUserRow, ChatTarget, Repo
from bot.handlers.hltb import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_RESULTS_LIMIT,
    PAGE_SIZE_KEY,
    RESULTS_LIMIT_KEY,
)
from bot.handlers.keyboards import (
    COMMON_OFFSETS_HOURS,
    format_offset,
    format_rarity,
    next_rarity_mode,
)
from bot.poller.daily import DEFAULT_TABLE_TOP, TOP_LIMIT_KEY
from bot.poller.fetcher import Fetcher
from bot.poller.message_cleanup import DEFAULT_TTL_MINUTES as DEFAULT_SYSTEM_MESSAGE_TTL_MIN
from bot.poller.message_cleanup import TTL_SETTING_KEY as SYSTEM_MESSAGE_TTL_KEY
from bot.poller.online_refresh import DEFAULT_REFRESH_INTERVAL_MIN as DEFAULT_ONLINE_REFRESH_MIN
from bot.poller.online_refresh import DEFAULT_TTL_HOURS as DEFAULT_ONLINE_REFRESH_TTL_HOURS
from bot.poller.online_refresh import REFRESH_INTERVAL_KEY as ONLINE_REFRESH_INTERVAL_KEY
from bot.poller.online_refresh import TTL_HOURS_KEY as ONLINE_REFRESH_TTL_KEY
from bot.services.stats import counters_for, month_cutoff_utc, today_cutoff_utc
from bot.services.tables import truncate_name
from bot.util import humanize_ago, parse_utc_offset, utcnow

log = logging.getLogger(__name__)

router = Router(name="admin")

PAGE_SIZE = 8

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


@router.callback_query(F.data == "a:newusers")
async def new_user_defaults_menu(callback: CallbackQuery, repo: Repo) -> None:
    await _redraw(callback, *await _new_user_defaults(repo))


@router.callback_query(F.data == "a:defaultrarity")
async def default_rarity_cycle(callback: CallbackQuery, repo: Repo) -> None:
    current = await repo.get_app_setting(DEFAULT_RARITY_MODE_KEY, DEFAULT_RARITY_MODE_DEFAULT)
    assert current is not None
    mode = next_rarity_mode(current)
    await repo.set_app_setting(DEFAULT_RARITY_MODE_KEY, mode, callback.from_user.id)
    await _redraw(callback, *await _new_user_defaults(repo))


# ------------------------------------------------------ free-text numeric settings

# Row-cap settings (always global — no per-chat meaning) and the per-chat
# rare threshold share one "type a number, not a button" flow — free values
# a handful of preset buttons could not cover anyway. Keyed by tg_id ->
# (which setting, which chat — None for a global row-cap), so a stray digit
# typed by an admin who isn't in this flow is never mistaken for input, and
# the one regex handler below knows which validation and target apply.
_awaiting_input: dict[int, tuple[str, int | None]] = {}

RARE_THRESHOLD_MIN = 0.01
RARE_THRESHOLD_MAX = 100.0
LIMIT_MIN = 1
LIMIT_MAX = 50

# What a brand-new subscription starts at (Repo.subscribe) — used to be a
# flat DEFAULT 'all' baked into the subscriptions table (schema.sql), now an
# admin-configurable app_settings row instead, same cycling button/helpers
# the personal and per-chat toggles already use (keyboards.py) rather than
# the free-text numeric flow above — 'all'/'rare'/'hidden' isn't a number.
DEFAULT_RARITY_MODE_KEY = "default_rarity_mode"
DEFAULT_RARITY_MODE_DEFAULT = "all"

UNLIMITED_LABEL = "без ограничения"

# stats_games_limit's own (key, default) belong to handlers/chat.py by rights
# (same as the other five, each imported from wherever it actually lives) —
# but chat.py already imports IsAdmin from this module, so importing back
# from chat.py here would be circular. Duplicated on purpose, just this one.
_STATS_GAMES_LIMIT_KEY = "stats_games_limit"
_DEFAULT_STATS_GAMES_LIMIT = 15


@dataclass(frozen=True, slots=True)
class NumericSetting:
    """One row of the "type a number" admin flow (2026-09-05 refactor —
    replaces five parallel dicts, all keyed by the same setting names, with
    one). `zero_label` only matters when `min == 0`; a setting that doesn't
    allow 0 never reaches _format_limit's zero branch at all."""

    label: str
    default: int
    min: int = LIMIT_MIN
    max: int = LIMIT_MAX
    zero_label: str = UNLIMITED_LABEL


# Every admin-configurable count/limit/interval in the bot, one place —
# each (key, default) pair still lives with the code that actually falls
# back to it (imported above), so this registry can't drift from reality
# the way five hand-typed dicts eventually would have.
NUMERIC_SETTINGS: dict[str, NumericSetting] = {
    TOP_LIMIT_KEY: NumericSetting("Строк в /summary", DEFAULT_TABLE_TOP, min=0),
    # SPEC 1.6: both render into a <blockquote expandable>, not a fixed-width
    # table — an "unlimited" list fits there just fine, so these two alone
    # allow 0 for "no cap". Everything else below stays at min=1: a page
    # size or a search pool of 0 is just broken, not "show everything".
    _STATS_GAMES_LIMIT_KEY: NumericSetting(
        "Игр в /stats", _DEFAULT_STATS_GAMES_LIMIT, min=0
    ),
    RESULTS_LIMIT_KEY: NumericSetting("Результатов поиска и подсказок HLTB", DEFAULT_RESULTS_LIMIT),
    # Feeds Telegram inline-keyboard rows directly — 50 buttons on one page
    # would be unusable, unlike the two above.
    PAGE_SIZE_KEY: NumericSetting("Результатов на странице (HLTB)", DEFAULT_PAGE_SIZE, max=10),
    # These two's own 0 means something else again — "off", not "no cap".
    SYSTEM_MESSAGE_TTL_KEY: NumericSetting(
        "Автоудаление системных сообщений (мин)",
        DEFAULT_SYSTEM_MESSAGE_TTL_MIN,
        min=0,
        max=60,
        zero_label="выключено",
    ),
    ONLINE_REFRESH_INTERVAL_KEY: NumericSetting(
        "Интервал автообновления /online (мин)",
        DEFAULT_ONLINE_REFRESH_MIN,
        min=0,
        max=60,
        zero_label="выключено",
    ),
    # Stays at the default min (1): a 0-hour window is just "off" spelled a
    # more confusing way than the interval's own off switch already is.
    ONLINE_REFRESH_TTL_KEY: NumericSetting(
        "Автообновление /online, часов", DEFAULT_ONLINE_REFRESH_TTL_HOURS, max=24
    ),
}


def _format_limit(key: str, value: str) -> str:
    spec = NUMERIC_SETTINGS[key]
    return spec.zero_label if value == "0" else value


@router.callback_query(F.data == "a:limits")
async def limits_menu(callback: CallbackQuery, repo: Repo) -> None:
    builder = InlineKeyboardBuilder()
    for key, spec in NUMERIC_SETTINGS.items():
        current = await repo.get_app_setting(key, str(spec.default))
        builder.row(
            InlineKeyboardButton(
                text=f"{spec.label}: {_format_limit(key, current)} ▸",
                callback_data=f"a:limit:{key}",
            )
        )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:home"))
    await _redraw(
        callback,
        "Лимиты строк в списках.\n\n"
        "Списки /summary и /stats можно сделать безлимитными (0) — они и так "
        "лежат в сворачиваемой цитате, урезать нечего.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("a:limit:"))
async def limit_menu(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    key = callback.data.rsplit(":", 1)[1]
    spec = NUMERIC_SETTINGS[key]
    current = await repo.get_app_setting(key, str(spec.default))
    _awaiting_input[callback.from_user.id] = (key, None)
    zero_hint = f" (0 — {spec.zero_label})" if spec.min == 0 else ""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="a:limits"))
    await _redraw(
        callback,
        f"{spec.label}: {_format_limit(key, current)}\n\n"
        f"Пришли новое значение целым числом, от {spec.min} до {spec.max}{zero_hint}.",
        builder.as_markup(),
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(r"^\d+([.,]\d+)?$"))
async def numeric_setting_input(message: Message, repo: Repo, fetcher: Fetcher) -> None:
    assert message.from_user is not None and message.text is not None
    pending = _awaiting_input.get(message.from_user.id)
    if pending is None:
        return  # a plain number from an admin who isn't in this flow — ignore
    key, chat_id = pending

    if key == "rare_threshold_percent":
        assert chat_id is not None  # only ever chat-scoped now (SPEC 5.5)
        value = float(message.text.replace(",", "."))
        if not (RARE_THRESHOLD_MIN <= value <= RARE_THRESHOLD_MAX):
            await message.answer(
                f"Число должно быть от {RARE_THRESHOLD_MIN} до {RARE_THRESHOLD_MAX}. Ещё раз?"
            )
            return
        del _awaiting_input[message.from_user.id]
        await repo.update_chat_settings(chat_id, rare_threshold_percent=value)
        reply_text, markup = await _chat(repo, chat_id)
        await message.answer(f"Порог редкости: {value:g}%\n\n{reply_text}", reply_markup=markup)
        return

    # The row-cap settings below are always global — chat_id is always None
    # here, there is no per-chat meaning for them.
    if "." in message.text or "," in message.text:
        await message.answer("Здесь только целое число. Ещё раз?")
        return
    value_int = int(message.text)
    spec = NUMERIC_SETTINGS[key]
    if not (spec.min <= value_int <= spec.max):
        await message.answer(f"Число должно быть от {spec.min} до {spec.max}. Ещё раз?")
        return
    stored = str(value_int)
    confirm = f"{spec.label}: {_format_limit(key, stored)}"

    del _awaiting_input[message.from_user.id]
    await repo.set_app_setting(key, stored, message.from_user.id)
    reply_text, markup = await _home(repo, fetcher)
    await message.answer(f"{confirm}\n\n{reply_text}", reply_markup=markup)


# ------------------------------------------------------- per-chat settings

# Rare threshold, daily-summary time and its timezone are always explicit
# per chat (SPEC 5.5, 5.7) — no global screen any more, editing always
# happens from a chat's own card.


def _hour_grid_markup(
    current: str, set_prefix: str, tz_callback: str, back_callback: str
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for hour in range(24):
        label = f"{hour:02d}"
        mark = "• " if current.startswith(label) else ""
        builder.add(
            InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"{set_prefix}{hour}")
        )
    builder.adjust(6)
    builder.row(InlineKeyboardButton(text="Часовой пояс ▸", callback_data=tz_callback))
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=back_callback))
    return builder.as_markup()


def _tz_grid_markup(
    current_minutes: int, set_prefix: str, manual_callback: str, back_callback: str
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for hours in COMMON_OFFSETS_HOURS:
        minutes = hours * 60
        mark = "• " if minutes == current_minutes else ""
        builder.add(
            InlineKeyboardButton(
                text=f"{mark}{format_offset(minutes)}", callback_data=f"{set_prefix}{minutes}"
            )
        )
    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="✏️ Ввести вручную", callback_data=manual_callback))
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=back_callback))
    return builder.as_markup()


@router.callback_query(F.data.startswith("a:crt:"))
async def chat_rare_menu(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    _awaiting_input[callback.from_user.id] = ("rare_threshold_percent", chat_id)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=f"a:chat:{chat_id}"))
    await _redraw(
        callback,
        f"Порог «редкого» достижения в «{chat.title or chat_id}»: "
        f"{chat.rare_threshold_percent:g}%\n\n"
        "Пришли новое значение одним числом, например 12 или 7.5 — от 0 до 100. "
        "Действует только на этот чат.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("a:ctime:"))
async def chat_time_menu(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await _redraw(
        callback,
        f"Время итога дня в «{chat.title or chat_id}»: {chat.daily_summary_time}",
        _hour_grid_markup(
            chat.daily_summary_time, f"a:ctimes:{chat_id}:", f"a:ctz:{chat_id}", f"a:chat:{chat_id}"
        ),
    )


@router.callback_query(F.data.startswith("a:ctimes:"))
async def chat_time_set(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    _, _, chat_id_raw, hour_raw = callback.data.split(":")
    chat_id, hour = int(chat_id_raw), int(hour_raw)
    await repo.update_chat_settings(chat_id, daily_summary_time=f"{hour:02d}:00")
    await callback.answer(f"Итог дня в {hour:02d}:00")
    await _redraw(callback, *await _chat(repo, chat_id))


@router.callback_query(F.data.startswith("a:ctz:"))
async def chat_zone_menu(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await _redraw(
        callback,
        f"Часовой пояс «{chat.title or chat_id}»: {format_offset(chat.tz_offset_min)}",
        _tz_grid_markup(
            chat.tz_offset_min, f"a:ctzs:{chat_id}:", f"a:ctzm:{chat_id}", f"a:ctime:{chat_id}"
        ),
    )


@router.callback_query(F.data.startswith("a:ctzs:"))
async def chat_zone_set(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    _, _, chat_id_raw, minutes_raw = callback.data.split(":")
    chat_id, minutes = int(chat_id_raw), int(minutes_raw)
    await repo.update_chat_settings(chat_id, tz_offset_min=minutes)
    await callback.answer(format_offset(minutes))
    await _redraw(callback, *await _chat(repo, chat_id))


@router.callback_query(F.data.startswith("a:ctzm:"))
async def chat_zone_manual_prompt(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    _awaiting_input[callback.from_user.id] = ("tz_offset_min", chat_id)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=f"a:ctz:{chat_id}"))
    await _redraw(
        callback,
        f"Часовой пояс «{chat.title or chat_id}»: {format_offset(chat.tz_offset_min)}\n\n"
        "Пришли смещение одним сообщением, со знаком: например +3, -5 или +5:30.",
        builder.as_markup(),
    )


@router.message(
    F.chat.type == ChatType.PRIVATE, F.text.regexp(r"(?i)^(?:utc)?\s*[+-]\d{1,2}(?::[0-5]\d)?$")
)
async def chat_timezone_input(message: Message, repo: Repo) -> None:
    assert message.from_user is not None and message.text is not None
    pending = _awaiting_input.get(message.from_user.id)
    if pending is None or pending[0] != "tz_offset_min":
        return  # a stray signed number from an admin not in this flow — ignore
    _, chat_id = pending
    assert chat_id is not None

    minutes = parse_utc_offset(message.text)
    if minutes is None:  # out of −12..+14 range — the regex alone can't catch that
        await message.answer("Это не похоже на реальный часовой пояс. Например: +3 или -5:30.")
        return

    del _awaiting_input[message.from_user.id]
    await repo.update_chat_settings(chat_id, tz_offset_min=minutes)
    reply_text, markup = await _chat(repo, chat_id)
    await message.answer(
        f"Часовой пояс: {format_offset(minutes)}\n\n{reply_text}", reply_markup=markup
    )


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


@router.callback_query(F.data.startswith("a:cdellast:"))
async def chat_delete_last(callback: CallbackQuery, repo: Repo, bot: Bot) -> None:
    """The admin panel's own way in to /delete_last's logic (chat.py) —
    found live: an admin looking to undo the bot's last message in a chat
    went looking for it here first, not the group chat itself. Same target
    (the last *non-system* message, 2026-09-05) and same "expected failure,
    forget the row either way" handling, just reached from the chat card
    instead of typed into the chat."""
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    message_id = await repo.last_non_system_bot_message(chat_id)
    if message_id is None:
        await callback.answer("Не нашёл сообщений бота в этом чате.", show_alert=True)
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        log.info("admin delete_last failed for chat %s message %s", chat_id, message_id)
        await repo.forget_bot_messages(chat_id, [message_id])
        await callback.answer(
            "Не смог удалить — возможно, сообщение слишком старое.", show_alert=True
        )
        return
    await repo.forget_bot_messages(chat_id, [message_id])
    await callback.answer("Удалил последнее сообщение.")


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


# A narrower sibling of the unconditional wipe above (2026-09-05 follow-up,
# "system message" auto-delete): these two leave published achievements,
# stats and summaries untouched, so they're safe as a routine cleanup, not
# just a "just in case" tool — one bounded to 24h, one with no time limit
# at all for whenever that isn't enough.


async def _system_wipe_prompt(
    callback: CallbackQuery, repo: Repo, chat_id: int, ids: list[int], confirm_callback: str
) -> None:
    chat = await _find_chat(repo, chat_id)
    if chat is None:
        await callback.answer("Чат не найден", show_alert=True)
        return
    if not ids:
        await callback.answer("Системных сообщений не нашёл.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, стереть", callback_data=confirm_callback))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data=f"a:chat:{chat_id}"))
    await _redraw(
        callback,
        f"Стереть {len(ids)} системных сообщений в «{chat.title or chat_id}»?\n\n"
        "Достижений, /stats, /summary и итога дня это не касается — только "
        "промежуточные сообщения (подсказки, подтверждения, /help и т.п.).",
        builder.as_markup(),
    )


async def _system_wipe_confirm(
    callback: CallbackQuery, repo: Repo, bot: Bot, ids: list[int]
) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])

    ok = True
    for start in range(0, len(ids), 100):  # Bot API caps deleteMessages at 100 ids per call
        try:
            await bot.delete_messages(chat_id, ids[start : start + 100])
        except Exception:
            log.info("system wipe failed for chat %s, chunk at %s", chat_id, start)
            ok = False

    await repo.forget_bot_messages(chat_id, ids)
    await callback.answer("Готово." if ok else "Частично — что-то не далось стереть.")
    await _redraw(callback, *await _chat(repo, chat_id))


@router.callback_query(F.data.startswith("a:cswipe:"))
async def chat_system_wipe_prompt(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    since = utcnow() - timedelta(hours=WIPE_WINDOW_HOURS)
    ids = await repo.system_bot_messages_since(chat_id, since)
    await _system_wipe_prompt(callback, repo, chat_id, ids, f"a:cswipey:{chat_id}")


@router.callback_query(F.data.startswith("a:cswipey:"))
async def chat_system_wipe_confirm(callback: CallbackQuery, repo: Repo, bot: Bot) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    since = utcnow() - timedelta(hours=WIPE_WINDOW_HOURS)
    ids = await repo.system_bot_messages_since(chat_id, since)
    await _system_wipe_confirm(callback, repo, bot, ids)


@router.callback_query(F.data.startswith("a:cswipeall:"))
async def chat_system_wipe_all_prompt(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    ids = await repo.all_system_bot_messages(chat_id)
    await _system_wipe_prompt(callback, repo, chat_id, ids, f"a:cswipeally:{chat_id}")


@router.callback_query(F.data.startswith("a:cswipeally:"))
async def chat_system_wipe_all_confirm(callback: CallbackQuery, repo: Repo, bot: Bot) -> None:
    assert callback.data is not None
    chat_id = int(callback.data.rsplit(":", 1)[1])
    ids = await repo.all_system_bot_messages(chat_id)
    await _system_wipe_confirm(callback, repo, bot, ids)


# ------------------------------------------------------------------- screens


async def _home(repo: Repo, fetcher: Fetcher) -> tuple[str, InlineKeyboardMarkup]:
    users = await repo.admin_users()
    chats = await repo.admin_chats()

    active = sum(1 for u in users if u.token_status == "active" and not u.is_excluded)
    excluded = sum(1 for u in users if u.is_excluded)
    broken = sum(1 for u in users if u.token_status != "active")

    text = (
        "⚙️ Администрирование\n\n"
        f"Пользователей:  {len(users)} "
        f"({active} активных, {excluded} исключено, {broken} без входа)\n"
        f"Чатов:          {sum(1 for c in chats if c.is_active)}\n"
        f"API (достижения): {_format_api_usage(fetcher.api_usage())}\n\n"
        "Порог редкости и время итога дня — теперь в карточке каждого "
        "чата (раздел «Чаты»), не здесь: у каждого чата своё значение."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Новые пользователи ▸", callback_data="a:newusers")],
            [InlineKeyboardButton(text="Лимиты таблиц ▸", callback_data="a:limits")],
            [InlineKeyboardButton(text="Пользователи ▸", callback_data="a:users:0")],
            [InlineKeyboardButton(text="Чаты ▸", callback_data="a:chats")],
        ]
    )
    return text, keyboard


async def _new_user_defaults(repo: Repo) -> tuple[str, InlineKeyboardMarkup]:
    """Settings that only ever apply at the moment someone new subscribes —
    grouped on their own screen (2026-09-05 follow-up) rather than sitting
    on the home screen forever, since none of them affect anyone already
    subscribed. Just default_rarity_mode for now (SPEC 9, M-Steam-2e's own
    Repo.subscribe reads it) — the natural home for anything else of the
    same shape added later."""
    default_rarity_mode = await repo.get_app_setting(
        DEFAULT_RARITY_MODE_KEY, DEFAULT_RARITY_MODE_DEFAULT
    )
    assert default_rarity_mode is not None  # a default was given above

    text = (
        "👤 Новые пользователи — настройки по умолчанию\n\n"
        "Действует только на подписки, оформленные с этого момента — "
        "уже существующие не трогает."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Ачивки по умолчанию: {format_rarity(default_rarity_mode)} ▸",
                    callback_data="a:defaultrarity",
                )
            ],
            [InlineKeyboardButton(text="‹ Назад", callback_data="a:home")],
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

    lines += ["", "Колонки: когда был в сети · достижений сегодня / за месяц"]
    return "\n".join(lines), builder.as_markup()


async def _card(repo: Repo, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = await repo.get_user(tg_id)
    if user is None or not user.xuid:
        return "Пользователь не найден.", _back_home()

    counters = await counters_for(repo, tg_id)
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
    threshold_label = f"{chat.rare_threshold_percent:g}%"
    zone_label = format_offset(chat.tz_offset_min)
    text = (
        f"💬 {chat.title or chat_id}\n\n"
        f"Состояние:    {'активен' if chat.is_active else 'отключён'}\n"
        f"Публикуется:  {chat.subscribers} чел.\n"
        f"Порог редк.:  {threshold_label}\n"
        f"Итог дня:     {'да' if chat.daily_summary else 'нет'}, в {chat.daily_summary_time}\n"
        f"Часовой пояс: {zone_label}\n"
        f"Мин. G:       {chat.min_gamerscore}\n\n"
        + ("Подписаны: " + ", ".join(names) if names else "Подписанных пока нет.")
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"Порог редкости: {threshold_label} ▸", callback_data=f"a:crt:{chat_id}"
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
            text=f"Время итога: {chat.daily_summary_time} ({zone_label}) ▸",
            callback_data=f"a:ctime:{chat_id}",
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
            text="🗑 Удалить последнее сообщение", callback_data=f"a:cdellast:{chat_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🧹 Стереть сообщения бота (24ч)", callback_data=f"a:cwipe:{chat_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🧹 Удалить системные (24ч)", callback_data=f"a:cswipe:{chat_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🧹 Удалить все системные", callback_data=f"a:cswipeall:{chat_id}"
        )
    )
    builder.row(InlineKeyboardButton(text="‹ К списку", callback_data="a:chats"))
    return text, builder.as_markup()


# ------------------------------------------------------------------- helpers


async def _find_chat(repo: Repo, chat_id: int) -> ChatTarget | None:
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
