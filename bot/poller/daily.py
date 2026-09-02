"""Daily summary (SPEC 5.7, format 7.3).

Counted entirely from the database — zero API calls. The job wakes up every
minute and asks "is it time yet", because the hour is a setting the admin can
change at runtime; a cron trigger would have to be rebuilt on every change.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from html import escape as html_escape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError

from bot.db.repo import ChatMemberStat, Repo
from bot.services.achievements import plural_achievements
from bot.services.stats import global_offset_minutes, local_now, month_start_utc
from bot.util import thousands, utcnow

log = logging.getLogger(__name__)

MONTH_TOP = 10
DAY_WINDOW_HOURS = 24
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

# Xbox gamertags run up to 15 characters (the unique-suffix form a bit more);
# truncating keeps every row on one line even in a narrow mobile view.
NAME_WIDTH = 15


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

            text = await build_summary(
                self._repo, chat.chat_id, offset, threshold, now_local.date()
            )
            if text is None:
                # Nobody unlocked anything: staying silent keeps the summary
                # meaningful instead of turning it into daily noise (SPEC 5.7).
                await self._repo.mark_daily_report_sent(chat.chat_id, report_date)
                continue

            try:
                await self._bot.send_message(chat.chat_id, text, parse_mode=ParseMode.HTML)
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


async def build_summary(
    repo: Repo, chat_id: int, offset: int, threshold: float, today: date
) -> str | None:
    """The same rolling-24h report used by the scheduled job and by /summary
    on demand — one implementation, one set of numbers (SPEC 5.7, 6.3).

    Rendered as an HTML <pre> block: Telegram's monospace font is the only way
    to get columns that actually line up, so the numbers read as a table
    instead of a run-on sentence of digits.
    """
    # A rolling 24 hours, not the calendar day. The scheduled summary goes out
    # at 23:00, so a calendar window would leave 23:00–00:00 in no report at
    # all — every day quietly lost its last hour. It also sidesteps the
    # timezone question, since members live in different ones.
    day_rows = await repo.chat_member_stats(
        chat_id, utcnow() - timedelta(hours=DAY_WINDOW_HOURS), threshold
    )
    if not day_rows:
        return None

    total = sum(row.count for row in day_rows)
    score = sum(row.score for row in day_rows)

    lines = [
        f"📊 <b>Итоги за сутки</b>, {today.day} {MONTHS[today.month - 1]}",
        "",
        _table(day_rows, ranked=False),
        f"Всего за сутки: {plural_achievements(total)}, +{thousands(score)} G",
    ]

    month_rows = await repo.chat_member_stats(chat_id, month_start_utc(offset), threshold)
    if month_rows:
        lines += ["", "<b>За месяц:</b>", _table(month_rows[:MONTH_TOP], ranked=True)]
    return "\n".join(lines)


def _table(rows: list[ChatMemberStat], *, ranked: bool) -> str:
    """A monospace column block: player, achievement count, gamerscore, and a
    diamond count for rare ones (SPEC 7.3)."""
    header = (
        f"{'#':>2} {'Игрок':<{NAME_WIDTH}} {'Ач.':>4} {'+G':>9}"
        if ranked
        else f"{'Игрок':<{NAME_WIDTH}} {'Ач.':>4} {'+G':>9}  💎"
    )
    body = [_table_row(row, place if ranked else None) for place, row in enumerate(rows, start=1)]
    return "<pre>" + "\n".join([header, *body]) + "</pre>"


def _table_row(row: ChatMemberStat, place: int | None) -> str:
    name = html_escape(_name(row)[:NAME_WIDTH].ljust(NAME_WIDTH))
    count = str(row.count).rjust(4)
    score = f"+{thousands(row.score)}".rjust(9)
    if place is not None:
        return f"{place:>2} {name} {count} {score}"
    # The diamond count counts rare ones by the current threshold; the
    # summary includes achievements the feed filtered out — it is a report,
    # not the feed (SPEC 7.3).
    rare = f"  💎{row.rare}" if row.rare else ""
    return f"{name} {count} {score}{rare}"


def _name(row: ChatMemberStat) -> str:
    return row.gamertag or f"id{row.tg_id}"
