"""Group commands (SPEC 6.3). UI only — no SQL outside repo, no API calls."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from html import escape as html_escape
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

from bot.db.repo import ChatPresenceRow, PlatformLink, RecentAchievement, Repo, TopGame, User
from bot.handlers.admin import IsAdmin
from bot.poller.daily import build_summary, full_leaderboard
from bot.services.achievements import platform_breakdown_suffix, plural_achievements, rarity_badge
from bot.services.stats import counters_for, local_now
from bot.services.tables import blockquote, truncate_name
from bot.util import cooldown_minutes_left, humanize_ago, thousands, utcnow

log = logging.getLogger(__name__)

router = Router(name="chat")

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

# Per chat, not per person: rate-limiting only the requester would still let a
# whole chat spam it in turns.
SUMMARY_COOLDOWN_SECONDS = 600
_last_summary: dict[int, float] = {}

# subscribe/unsubscribe is a check-then-act (is_subscribed, then write) —
# without a lock, a fast confirm-then-/subscribe (or a double-tapped button)
# could interleave across two concurrently-handled updates and read stale
# state. Same pattern as the per-user lock around token refresh
# (bot/services/xbox/auth.py) — keyed by (chat_id, tg_id), not just tg_id,
# since a subscription is scoped to one chat.
_subscription_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _subscription_lock(chat_id: int, tg_id: int) -> asyncio.Lock:
    return _subscription_locks.setdefault((chat_id, tg_id), asyncio.Lock())


RECENT_GAMES_DAYS = 30
RECENT_DEFAULT = 5
RECENT_MAX = 20


class UsernameMiddleware(BaseMiddleware):
    """Keep users.username fresh, and note who's been seen writing in a group
    (`chat_seen` — feeds /online, SPEC 6.3).

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
        if isinstance(event, Message) and event.from_user:
            if event.from_user.username:
                await self._repo.update_username(event.from_user.id, event.from_user.username)
            if event.chat.type in GROUP_TYPES:
                await self._repo.record_chat_seen(event.chat.id, event.from_user.id)
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
            f"Сначала подключи XBOX в личке: https://t.me/{me.username}?start=connect"
        )
        return

    await repo.upsert_chat(message.chat.id, message.chat.title, message.from_user.id)
    async with _subscription_lock(message.chat.id, message.from_user.id):
        if await repo.is_subscribed(message.chat.id, message.from_user.id):
            await message.answer("Ты уже публикуешься здесь.")
            return
        await repo.subscribe(message.chat.id, message.from_user.id)
    await message.answer(
        f"Готово. Ачивки {user.gamertag or 'твои'} будут прилетать сюда.\n"
        "Настройки редкости и XBOX 360 — в личке, /panel."
    )


@router.message(Command("unsubscribe"))
async def unsubscribe(message: Message, repo: Repo) -> None:
    """Same weight as /disconnect_xbox: losing your feed in a chat you might not
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
    await message.answer(
        "Перестать публиковать твои достижения в этом чате?", reply_markup=keyboard
    )


@router.callback_query(F.data == "unsub:no")
async def unsubscribe_cancel(callback: CallbackQuery) -> None:
    # Nothing changed — remove the prompt instead of leaving a "cancelled"
    # message in the group chat for no reason.
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.delete()
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
    async with _subscription_lock(callback.message.chat.id, tg_id):
        await repo.unsubscribe(callback.message.chat.id, tg_id)
    await callback.message.edit_text("Больше не публикую твои достижения в этом чате.")
    await callback.answer()


# -------------------------------------------------------------------- stats


def _games_list(games: list[TopGame]) -> str:
    rows = [
        f"{place}. {_PLATFORM_ICON.get(game.platform, '')} "
        f"{html_escape(truncate_name(game.name or 'без названия'))} — "
        f"{game.unlocked or 0} ач. (+{thousands(game.gamerscore or 0)} G)"
        for place, game in enumerate(games, start=1)
    ]
    return blockquote(rows)


# Marker circles for the platform lines in /stats' header, and for
# /online's per-name icon while online (_presence_icon below, SPEC 9,
# M-Steam-2e — offline/no-data there stays grey, not platform-coloured).
# PlayStation isn't linkable yet, kept for when it is.
_PLATFORM_ICON = {"modern": "🟢", "steam": "⚫", "psn": "🔵"}
_PLATFORM_LABEL = {"steam": "Steam", "psn": "PlayStation"}


def _display_name(target: User, links: list[PlatformLink]) -> str:
    if target.gamertag:
        return target.gamertag
    if links:
        return links[0].display_name or links[0].external_id
    return "без геймертега"


async def _build_stats_text(repo: Repo, target: User) -> str | None:
    """Shared by /stats and /who's buttons (SPEC 6.3) — one implementation,
    so a player's card looks the same no matter how it was opened.

    Works for a Steam-only person too (SPEC 9, M-Steam-2e) — used to bail
    out on `not target.xuid` alone, which meant no card at all for anyone
    without Xbox connected."""
    platform_links = await repo.platform_links_of(target.tg_id)
    if not target.xuid and not platform_links:
        return None

    counters = await counters_for(repo, target.tg_id)
    lines = [f"📊 <b>{html_escape(_display_name(target, platform_links))}</b>"]
    if target.xuid:
        lines.append(
            f"{_PLATFORM_ICON['modern']} XBOX: {html_escape(target.gamertag or 'без геймертега')}"
            f"  ·  gamerscore {thousands(target.gamerscore or 0)}"
        )
    for link in platform_links:
        icon = _PLATFORM_ICON.get(link.platform, "⚪")
        label = _PLATFORM_LABEL.get(link.platform, link.platform)
        # A lifetime count is fine here, unlike Xbox's own seen_achievements
        # count above (deliberately never shown as a lifetime total, SPEC
        # 5.4: title_history is capped, so any count derived from it could
        # undercount) — a Steam backfill has no such cap, GetOwnedGames
        # sees the whole library, so this number is trustworthy as-is.
        count = await repo.platform_achievement_count(target.tg_id, link.platform)
        lines.append(
            f"{icon} {label}: {html_escape(link.display_name or link.external_id)}"
            f"  ·  {plural_achievements(count)}"
        )

    today_breakdown = platform_breakdown_suffix(counters.today_xbox, counters.today_steam)
    month_breakdown = platform_breakdown_suffix(counters.month_xbox, counters.month_steam)
    lines += [
        "",
        f"Сегодня:   {plural_achievements(counters.today)}{today_breakdown}"
        f" (+{counters.today_score} G)",
        f"За месяц:  {plural_achievements(counters.month)}{month_breakdown}"
        f" (+{thousands(counters.month_score)} G)",
        # No lifetime "Всего" here: seen_achievements is permanently
        # best-effort (title_history's cap, achievements with no unlock
        # date), so a lifetime count from it can't be trusted the way a
        # date-bounded one can — better absent than quietly wrong (SPEC 5.4).
    ]

    # Found live, long-standing gap: this used to be Xbox-only (SPEC 9,
    # M-Steam-2c scoped it out for lack of a Steam recently-played source —
    # recent_games() itself was never Xbox-specific, just never called for
    # anything else). One combined ranked list, not a section per platform —
    # same "one number, not one per platform" spirit as the counters above.
    external_ids = [target.xuid] if target.xuid else []
    external_ids += [link.external_id for link in platform_links]
    if external_ids:
        # 0 = no cap (SPEC 6.4) — the list lives in a collapsible quote
        # either way, no separate "показать все игры" tap needed any more.
        limit = await _stats_games_limit(repo)
        since = utcnow() - timedelta(days=RECENT_GAMES_DAYS)
        per_source = await asyncio.gather(
            *(repo.recent_games(external_id, since, limit=limit) for external_id in external_ids)
        )
        games = sorted(
            (game for source in per_source for game in source),
            key=lambda g: (g.gamerscore or 0, g.unlocked or 0),
            reverse=True,
        )[: limit or None]
        if games:
            lines += ["", f"<b>Игры за {RECENT_GAMES_DAYS} дней</b>", _games_list(games)]
    return "\n".join(lines)


async def _stats_games_limit(repo: Repo) -> int:
    raw = await repo.get_app_setting("stats_games_limit", "15")
    try:
        return int(raw or 15)
    except ValueError:
        return 15


@router.message(Command("stats"))
async def stats(message: Message, repo: Repo, command: CommandObject) -> None:
    target = await _resolve(message, repo, command.args)
    if target is None:
        await message.answer(_UNKNOWN)
        return
    text = await _build_stats_text(repo, target)
    if text is None:
        await message.answer("Этот человек ещё ничего не подключил.")
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
    # Platform colour while online (SPEC 9, M-Steam-2e; same palette as
    # /stats) — but grey for offline/no data regardless of platform.
    # Found live: a pure platform colour made every offline row look the
    # same as an online one at a glance, losing the one signal a colour
    # is actually good for — grey is the "nothing to see here" cue, and
    # that's true the same way on every platform.
    if row.state != "Online":
        return "⚪"
    return _PLATFORM_ICON.get(row.platform, "⚪")


@router.message(Command("online"))
async def online(message: Message, repo: Repo) -> None:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Список игроков — по чату, набери команду в группе.")
        return

    rows = await repo.chat_member_presence(message.chat.id)
    if not rows:
        await message.answer("Никого из подключённых в этом чате пока не видел.")
        return

    # Plain text, no per-name buttons: a keyboard row per player stops being a
    # list and starts being a second keyboard once a chat has more than a
    # few people. Picking someone to look up is /who's job, not this one's.
    lines = ["🎮 <b>Онлайн-статус игроков</b>", ""]
    for row in rows:
        name = row.gamertag or f"id{row.tg_id}"
        lines.append(f"{_presence_icon(row)} {name} — {_presence_text(row)}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("who"))
async def who(message: Message, repo: Repo) -> None:
    """The picker /online used to double as (SPEC 6.3) — split out so /online
    can stay a plain glance and this can stay a plain button grid."""
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Список игроков — по чату, набери команду в группе.")
        return

    rows = await repo.chat_member_presence(message.chat.id)
    if not rows:
        await message.answer("Никого из подключённых в этом чате пока не видел.")
        return

    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.button(
            text=row.gamertag or f"id{row.tg_id}", callback_data=f"who:stats:{row.tg_id}"
        )
    builder.adjust(3)
    # Found live: no way out except picking someone, and the prompt itself
    # never went away after a pick — just sat there stale.
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="who:cancel"))
    await message.answer("Чья статистика интересует?", reply_markup=builder.as_markup())


@router.callback_query(F.data == "who:cancel")
async def who_cancel(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("who:stats:"))
async def who_stats_button(callback: CallbackQuery, repo: Repo) -> None:
    assert callback.data is not None
    tg_id = int(callback.data.rsplit(":", 1)[1])
    target = await repo.get_user(tg_id)
    if target is None:
        await callback.answer("Не нашёл такого пользователя.", show_alert=True)
        return
    text = await _build_stats_text(repo, target)
    await callback.answer()
    if isinstance(callback.message, Message):
        if text is not None:
            await callback.message.answer(text, parse_mode=ParseMode.HTML)
        # The picker's own job is done either way — drop it instead of
        # leaving a stale "Чья статистика интересует?" behind.
        with contextlib.suppress(Exception):
            await callback.message.delete()


# ------------------------------------------------------------------- summary


async def _summary_or_cooldown(
    repo: Repo, chat_id: int
) -> tuple[str | None, InlineKeyboardMarkup | None, int]:
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
        return None, None, minutes_left

    # The cooldown covers "nothing new" too — that answer is still a message,
    # and without this it could be spammed just as freely as a real summary.
    _last_summary[chat_id] = time.monotonic()
    settings_row = await repo.get_chat_daily_settings(chat_id)
    built = await build_summary(
        repo,
        chat_id,
        settings_row.rare_threshold_percent,
        local_now(settings_row.tz_offset_min).date(),
    )
    if built is None:
        return None, None, 0
    text, markup = built
    return text, markup, 0


@router.message(Command("summary"))
async def summary_command(message: Message, repo: Repo) -> None:
    """The same report the scheduled job sends, on demand."""
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Сводка считается по чату — набери команду в группе.")
        return

    text, markup, minutes_left = await _summary_or_cooldown(repo, message.chat.id)
    if minutes_left:
        await message.answer(f"Сводку уже присылали недавно. Ещё раз — через {minutes_left} мин.")
        return
    if text is None:
        await message.answer("За последние сутки в чате пока никто ничего не выбил.")
        return
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@router.callback_query(F.data.startswith("summary:all:"))
async def summary_show_all(callback: CallbackQuery, repo: Repo) -> None:
    """«Показать всех» under a truncated summary table — a fresh, uncapped
    re-fetch as its own message, not the original send re-edited (SPEC 6.3)."""
    if not isinstance(callback.message, Message):
        return
    assert callback.data is not None
    window = callback.data.rsplit(":", 1)[1]
    settings_row = await repo.get_chat_daily_settings(callback.message.chat.id)
    text = await full_leaderboard(
        repo, callback.message.chat.id, settings_row.rare_threshold_percent, window
    )
    await callback.answer()
    if text is not None:
        await callback.message.answer(text, parse_mode=ParseMode.HTML)


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
    text = "🕘 <b>Последние достижения</b>\n" + _recent_list(rows)
    await message.answer(text, parse_mode=ParseMode.HTML)


def _recent_list(rows: list[RecentAchievement]) -> str:
    return blockquote([_recent_row(row) for row in rows])


def _recent_row(row: RecentAchievement) -> str:
    # A real Telegram spoiler works fine inside a blockquote (unlike the old
    # <pre> table it replaced, SPEC 7.1) — the real name stays hidden behind
    # a tap, instead of a placeholder that gave nothing away to look up.
    name = html_escape(truncate_name(row.name))
    if row.is_secret:
        name = f'<span class="tg-spoiler">{name}</span>'
    # Leads the line instead of a fixed "🏆" bullet (2026-09-05) — now that
    # rarity_badge() always returns something (diamond or cup, never
    # empty), a separate generic bullet would double up with it on every
    # "common" row: two trophies back to back on the same line.
    badge = rarity_badge(row.rarity_percent)
    gamertag = html_escape(truncate_name(row.gamertag or "кто-то"))
    game = html_escape(truncate_name(row.game or "без названия"))
    return (
        f"{badge} {gamertag} — {name}, {game} (+{thousands(row.gamerscore)} G)"
        f" · {humanize_ago(row.unlocked_at)}"
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


# Rewritten 2026-09-05: no connect/subscribe walkthrough in the text any
# more — the hub's own buttons (hub_keyboard, below) already cover both,
# intuitively enough on their own that spelling them out here was just
# noise by comparison to what people actually come back to read: what the
# bot is, and the commands.
HELP_TEXT = (
    "🎮 Слежу за достижениями тех, кто играет на XBOX и в Steam, и "
    "публикую их сюда — с фильтром по редкости, статистикой каждого и "
    "итогом дня.\n\n"
    "Команды чата:\n"
    "/stats [@кто] — статистика: без аргумента своя, с ником — чужая\n"
    "/who — узнать стату конкретного игрока\n"
    "/online — кто сейчас в игре\n"
    "/recent [N] — последние достижения чата\n"
    "/summary — сводка за сутки и за месяц\n"
    "/hltb — показать сводку игры HowLongToBeat\n\n"
    "Настройки — в личке, /panel."
)


def hub_keyboard(bot_username: str, chat_id: int) -> InlineKeyboardMarkup:
    """A short walkthrough, not a control panel: SPEC 6.3 walks through
    connect → publish in that order, so the keyboard should not offer more
    choices than that story needs. Steam's connect button (SPEC 9,
    M-Steam-2e) sits next to Xbox's rather than adding a whole extra row —
    it is still the same "connect" step, just a second platform for it.

    Buttons act on whoever presses them — that is why "Публиковать мои
    достижения" is allowed here at all: SPEC 6.3 forbids rendering *someone
    else's* settings where any member could page through them, not a button
    that only ever touches the presser's own subscription.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Публиковать мои достижения", callback_data="sub:on")],
            [
                InlineKeyboardButton(
                    text="🔗 Подключить XBOX",
                    # The chat id rides along in the deep-link payload so a
                    # successful login can auto-subscribe him right back here
                    # (SPEC 6.3) — see _parse_connect_payload in connect.py.
                    url=f"https://t.me/{bot_username}?start=connect{chat_id}",
                ),
                InlineKeyboardButton(
                    text="🎮 Подключить Steam",
                    # No chat id here (unlike Xbox above) — /connect_steam
                    # needs a profile link a button tap can't supply anyway,
                    # so this just opens the DM at the right prompt (SPEC 9,
                    # handlers/steam.py, connect.py's ?start=connectsteam).
                    url=f"https://t.me/{bot_username}?start=connectsteam",
                ),
            ],
            [
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
        # Don't just tell him to go connect somewhere — send him straight into
        # the same login deep link as the "Подключить XBOX" button. It carries
        # this chat's id, so ConnectService auto-subscribes here once he's
        # done (SPEC 6.3); no need to remember to come back and press this
        # button again.
        me = await bot.me()
        await callback.answer(url=f"https://t.me/{me.username}?start=connect{message.chat.id}")
        return

    await repo.upsert_chat(message.chat.id, message.chat.title, callback.from_user.id)
    async with _subscription_lock(message.chat.id, callback.from_user.id):
        if await repo.is_subscribed(message.chat.id, callback.from_user.id):
            await callback.answer("Ты уже публикуешься здесь.")
            return
        await repo.subscribe(message.chat.id, callback.from_user.id)
    await callback.answer("Готово, твои достижения будут прилетать сюда.")
    await _refresh_hub(message, repo, bot)


async def _refresh_hub(message: Message, repo: Repo, bot: Bot) -> None:
    me = await bot.me()
    with contextlib.suppress(Exception):
        # Telegram refuses an edit that changes nothing — not an error.
        await message.edit_text(
            await hub_text(repo, message.chat.id),
            reply_markup=hub_keyboard(me.username or "", message.chat.id),
        )


@router.message(Command("delete_last"), F.chat.type.in_(GROUP_TYPES), IsAdmin())
async def delete_last(message: Message, repo: Repo, bot: Bot) -> None:
    """Quick undo, right in the chat — the admin panel's own "стереть
    сообщения бота" (admin.py's a:cwipe) is a 24-hour bulk wipe reached
    through a private-chat menu, overkill for "oops, wrong one just now".
    IsAdmin (admin.py) is the bot's own admin_tg_ids, same as everywhere
    else "admin" means in this project — not generic Telegram chat admins.
    """
    message_id = await repo.last_bot_message(message.chat.id)
    if message_id is None:
        await message.answer("Не нашёл сообщений бота в этом чате.")
        return

    try:
        await bot.delete_message(message.chat.id, message_id)
    except Exception:
        # Too old (Telegram caps deletes at 48h) or already gone either way
        # — same reasoning as the bulk wipe, nothing left worth keeping the
        # log row for.
        log.info("delete_last failed for chat %s message %s", message.chat.id, message_id)
        await repo.forget_bot_messages(message.chat.id, [message_id])
        await message.answer("Не смог удалить — возможно, сообщение слишком старое.")
        return

    await repo.forget_bot_messages(message.chat.id, [message_id])
    with contextlib.suppress(Exception):
        await message.delete()  # tidy up the /delete_last command itself too
