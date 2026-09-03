"""Daily summary (SPEC 5.7, format 7.3).

Counted entirely from the database — zero API calls. The job wakes up every
minute and asks "is it time yet", because the hour is a setting the admin can
change at runtime; a cron trigger would have to be rebuilt on every change.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.repo import ChatMemberStat, Repo
from bot.services.achievements import plural_achievements
from bot.services.stats import global_offset_minutes, local_now
from bot.services.tables import render_table, total_line, truncate_name
from bot.util import thousands, utcnow

log = logging.getLogger(__name__)

DAY_WINDOW_HOURS = 24
# Rolling, like the day window — not the calendar month. Same reasoning: a
# calendar boundary would cut the window at an arbitrary moment and give every
# member a different "this month" depending on when they check.
MONTH_WINDOW_DAYS = 30
DEFAULT_TABLE_TOP = 15
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


class DailySummary:
    def __init__(self, bot: Bot, repo: Repo) -> None:
        self._bot = bot
        self._repo = repo

    async def tick(self) -> None:
        offset = await global_offset_minutes(self._repo)
        now_local = local_now(offset)
        target = await self._repo.get_app_setting("daily_summary_time", "23:00")
        if now_local.strftime("%H:%M") != target:
            return

        report_date = now_local.date().isoformat()
        threshold = await current_rare_threshold(self._repo)

        for chat in await self._repo.admin_chats():
            if not chat.is_active or not chat.daily_summary:
                continue
            if await self._repo.daily_report_sent(chat.chat_id, report_date):
                continue

            built = await build_summary(self._repo, chat.chat_id, threshold, now_local.date())
            if built is None:
                # Nobody unlocked anything: staying silent keeps the summary
                # meaningful instead of turning it into daily noise (SPEC 5.7).
                await self._repo.mark_daily_report_sent(chat.chat_id, report_date)
                continue
            text, markup = built

            try:
                await self._bot.send_message(
                    chat.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup
                )
            except TelegramForbiddenError:
                log.info("chat %s refused the summary, deactivating", chat.chat_id)
                await self._repo.deactivate_chat(chat.chat_id)
                continue
            except Exception:
                log.exception("could not send the daily summary to %s", chat.chat_id)
                continue
            await self._repo.mark_daily_report_sent(chat.chat_id, report_date)


async def current_rare_threshold(repo: Repo) -> float:
    raw = await repo.get_app_setting("rare_threshold_percent", "10")
    try:
        return float(raw or 10)
    except ValueError:
        return 10.0


async def current_top_limit(repo: Repo) -> int:
    raw = await repo.get_app_setting("summary_top_limit", str(DEFAULT_TABLE_TOP))
    try:
        return int(raw or DEFAULT_TABLE_TOP)
    except ValueError:
        return DEFAULT_TABLE_TOP


async def build_summary(
    repo: Repo, chat_id: int, threshold: float, today: date
) -> tuple[str, InlineKeyboardMarkup | None] | None:
    """The same two-window report used by the scheduled job and by /summary
    on demand — one implementation, one set of numbers (SPEC 5.7, 6.3).

    Both windows are rolling, not calendar-bound — a calendar day or month
    would cut off at an arbitrary moment and give every member a different
    "today" depending on when they happen to check (SPEC 5.7) — and both list
    everyone subscribed, zero-scorers included, so the table reads as a
    roster, not just whoever happened to unlock something.
    """
    now = utcnow()
    day_rows = await repo.chat_member_stats(
        chat_id, now - timedelta(hours=DAY_WINDOW_HOURS), threshold
    )
    if not day_rows or not any(row.count for row in day_rows):
        return None

    month_rows = await repo.chat_member_stats(
        chat_id, now - timedelta(days=MONTH_WINDOW_DAYS), threshold
    )

    top_limit = await current_top_limit(repo)
    day_lines, day_full = _section(day_rows, top_limit)
    lines = [f"📊 <b>24 часа</b>, {today.day} {MONTHS[today.month - 1]}", *day_lines]

    month_full = False
    if month_rows:
        month_lines, month_full = _section(month_rows, top_limit)
        lines += ["", "📊 <b>30 дней</b>", *month_lines]

    buttons = []
    if day_full:
        buttons.append(
            InlineKeyboardButton(text="Показать всех (24ч)", callback_data="summary:all:day")
        )
    if month_full:
        buttons.append(
            InlineKeyboardButton(text="Показать всех (30д)", callback_data="summary:all:month")
        )
    markup = InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons]) if buttons else None
    return "\n".join(lines), markup


async def full_leaderboard(repo: Repo, chat_id: int, threshold: float, window: str) -> str | None:
    """The uncapped table behind a summary's «Показать всех» button (SPEC
    6.3) — re-fetched fresh rather than carried over from the original send,
    same as /hltb's sessions do for their own "current data" reasons."""
    now = utcnow()
    hours = DAY_WINDOW_HOURS if window == "day" else MONTH_WINDOW_DAYS * 24
    rows = await repo.chat_member_stats(chat_id, now - timedelta(hours=hours), threshold)
    if not rows:
        return None
    lines, _ = _section(rows, limit=len(rows))  # limit=len(rows): never truncate here
    label = "24 часа" if window == "day" else "30 дней"
    return "\n".join([f"📊 <b>{label}, полностью</b>", *lines])


def _section(rows: list[ChatMemberStat], limit: int) -> tuple[list[str], bool]:
    total = sum(row.count for row in rows)
    score = sum(row.score for row in rows)
    table = render_table(
        ["#", "Игрок", "Ач.", "+G", "💎"],
        [_table_row(place, row) for place, row in enumerate(rows[:limit], start=1)],
        ["<", "<", ">", ">", ">"],
    )
    lines = [table, total_line("Всего", f"{plural_achievements(total)}, +{thousands(score)} G")]
    return lines, len(rows) > limit


def _table_row(place: int, row: ChatMemberStat) -> list[str]:
    return [
        str(place),
        truncate_name(row.gamertag or f"id{row.tg_id}"),
        str(row.count),
        f"+{thousands(row.score)}",
        str(row.rare) if row.rare else "",
    ]
