"""Keeps /online's table fresh on its own for a while (Follow-up 2026-09-05).

A person running /online used to get a one-off snapshot — this tick re-fetches
presence and edits the same message in place every `online_refresh_interval_
min` minutes, until `online_refresh_ttl_hours` after it was first posted
(both admin-configurable, global — one clock for the whole bot, same as
message_cleanup.py's own TTL). One row per chat (db/repo.py's
online_auto_refresh, PRIMARY KEY chat_id): a fresh /online supersedes
whatever was auto-refreshing before, the old message just goes stale.

Runs every tick (60s, same as everything else in scheduler.py) rather than
once a day as a separate job — checking age this often already satisfies
"don't let a stale auto-refresher keep firing requests" far more thoroughly
than a daily sweep would, so a second, coarser job would only ever find what
this one already caught a day earlier.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.enums import ParseMode

from bot.db.repo import OnlineAutoRefreshRow, Repo
from bot.services.message_log import stats_category
from bot.services.online_view import render_online_table
from bot.services.stats import local_now
from bot.util import utcnow

log = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_MIN = 10
DEFAULT_TTL_HOURS = 3
REFRESH_INTERVAL_KEY = "online_refresh_interval_min"
TTL_HOURS_KEY = "online_refresh_ttl_hours"


async def refresh_interval_minutes(repo: Repo) -> int:
    return await repo.get_int_setting(REFRESH_INTERVAL_KEY, DEFAULT_REFRESH_INTERVAL_MIN)


async def ttl_hours(repo: Repo) -> int:
    return await repo.get_int_setting(TTL_HOURS_KEY, DEFAULT_TTL_HOURS)


class OnlineAutoRefresh:
    def __init__(self, bot: Bot, repo: Repo) -> None:
        self._bot = bot
        self._repo = repo

    async def tick(self) -> None:
        interval = await refresh_interval_minutes(self._repo)
        ttl = await ttl_hours(self._repo)
        now = utcnow()
        for row in await self._repo.all_online_auto_refreshes():
            created_at = datetime.fromisoformat(row.created_at)
            # 0 disables the whole feature (admin's own off switch) — treat
            # every existing row as instantly expired rather than leaving it
            # to age out on its own, same "fail toward doing less" choice as
            # message_cleanup.py's own ttl <= 0 check.
            if ttl <= 0 or now - created_at >= timedelta(hours=ttl):
                await self._repo.delete_online_auto_refresh(row.chat_id)
                continue
            if interval <= 0:
                continue
            last_updated_at = datetime.fromisoformat(row.last_updated_at)
            if now - last_updated_at < timedelta(minutes=interval):
                continue
            await self._refresh_one(row)

    async def _refresh_one(self, row: OnlineAutoRefreshRow) -> None:
        rows = await self._repo.chat_member_presence(row.chat_id)
        if not rows:
            # Everyone unsubscribed/got excluded since — nothing left to
            # show, same as /online's own empty-chat reply. Stop rather
            # than edit into an empty table nobody asked to see age too.
            await self._repo.delete_online_auto_refresh(row.chat_id)
            return
        settings_row = await self._repo.get_chat_daily_settings(row.chat_id)
        updated_label = local_now(settings_row.tz_offset_min).strftime("%H:%M")
        text = render_online_table(rows, updated_label)
        try:
            with stats_category():
                await self._bot.edit_message_text(
                    chat_id=row.chat_id,
                    message_id=row.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            # Expected, not exceptional: the message was deleted (admin's
            # own bulk wipe, /delete_last, or by hand) or the bot got kicked
            # — nothing left worth retrying, same "forget it either way"
            # reasoning as message_cleanup.py. The timestamp line changes
            # every refresh, so a genuine no-op edit basically never happens.
            log.info("online auto-refresh: edit failed for chat %s, stopping", row.chat_id)
            await self._repo.delete_online_auto_refresh(row.chat_id)
            return
        await self._repo.touch_online_auto_refresh(row.chat_id)
