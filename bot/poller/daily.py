"""Daily summary (SPEC 5.7, format 7.3).

Counted entirely from the database — zero API calls. The job wakes up every
minute and asks "is it time yet", because the hour is a setting the admin can
change at runtime; a cron trigger would have to be rebuilt on every change.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from aiogram import Bot
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
        threshold = await self._rare_threshold()

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
                await self._bot.send_message(chat.chat_id, text)
            except TelegramForbiddenError:
                log.info("chat %s refused the summary, deactivating", chat.chat_id)
                await self._repo.deactivate_chat(chat.chat_id)
                continue
            except Exception:
                log.exception("could not send the daily summary to %s", chat.chat_id)
                continue
            await self._repo.mark_daily_report_sent(chat.chat_id, report_date)

    async def _rare_threshold(self) -> float:
        return await current_rare_threshold(self._repo)


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
    on demand — one implementation, one set of numbers (SPEC 5.7, 6.3)."""
    # A rolling 24 hours, not the calendar day. The scheduled summary goes out
    # at 23:00, so a calendar window would leave 23:00–00:00 in no report at
    # all — every day quietly lost its last hour. It also sidesteps the
    # timezone question, since members live in different ones.
    day_rows = await repo.chat_member_stats(
        chat_id, utcnow() - timedelta(hours=DAY_WINDOW_HOURS), threshold
    )
    if not day_rows:
        return None

    lines = [f"📊 Итоги за сутки, {today.day} {MONTHS[today.month - 1]}", ""]
    lines += [_day_line(row) for row in day_rows]

    total = sum(row.count for row in day_rows)
    score = sum(row.score for row in day_rows)
    lines += ["", f"Всего за сутки: {plural_achievements(total)}, +{thousands(score)} G"]

    month_rows = await repo.chat_member_stats(chat_id, month_start_utc(offset), threshold)
    if month_rows:
        lines += ["", "За месяц:"]
        for place, row in enumerate(month_rows[:MONTH_TOP], start=1):
            lines.append(f"{place}. {_name(row)}  {row.count}  +{thousands(row.score)} G")
    return "\n".join(lines)


def _day_line(row: ChatMemberStat) -> str:
    line = f"{_name(row)}  {row.count}  +{thousands(row.score)} G"
    # The diamond column counts rare ones by the current threshold; the summary
    # includes achievements the feed filtered out — it is a report (SPEC 7.3).
    return f"{line}  💎 {row.rare}" if row.rare else line


def _name(row: ChatMemberStat) -> str:
    return row.gamertag or f"id{row.tg_id}"
