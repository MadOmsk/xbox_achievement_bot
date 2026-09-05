"""APScheduler wiring: one tick a minute plus two housekeeping jobs.

APScheduler rather than a bare asyncio loop for `coalesce` and `max_instances`:
a tick that overruns must not pile up behind itself.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.db.repo import Repo
from bot.poller.daily import DailySummary
from bot.poller.fetcher import Fetcher
from bot.poller.message_cleanup import MessageCleanup
from bot.poller.presence import PresencePoller
from bot.poller.reminders import ReminderJob
from bot.poller.steam_presence import SteamPresencePoller

log = logging.getLogger(__name__)

TICK_SECONDS = 60


class PollerScheduler:
    def __init__(
        self,
        poller: PresencePoller,
        fetcher: Fetcher,
        reminders: ReminderJob,
        daily: DailySummary,
        repo: Repo,
        steam_poller: SteamPresencePoller,
        message_cleanup: MessageCleanup,
    ) -> None:
        self._poller = poller
        self._fetcher = fetcher
        self._reminders = reminders
        self._daily = daily
        self._repo = repo
        self._steam_poller = steam_poller
        self._message_cleanup = message_cleanup
        self._scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        self._scheduler.add_job(
            self._poller.tick,
            IntervalTrigger(seconds=TICK_SECONDS),
            id="presence",
            coalesce=True,
            max_instances=1,
        )
        # Registered unconditionally, same as reminders/daily_summary below —
        # the tick itself exits early when Steam isn't configured (SPEC 9,
        # M-Steam-2c), simpler than conditionally building the schedule.
        self._scheduler.add_job(
            self._steam_poller.tick,
            IntervalTrigger(seconds=TICK_SECONDS),
            id="steam_presence",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self._daily_history,
            CronTrigger(hour=4, minute=0),
            id="title_history",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self._reminders.run,
            IntervalTrigger(hours=6),
            id="reminders",
            coalesce=True,
            max_instances=1,
        )
        # Every minute, because the hour is a runtime setting: a cron trigger
        # would have to be rebuilt whenever the admin changes it (SPEC 5.7).
        self._scheduler.add_job(
            self._daily.tick,
            IntervalTrigger(seconds=TICK_SECONDS),
            id="daily_summary",
            coalesce=True,
            max_instances=1,
        )
        # Same cadence as everything else here — a message due at minute 5
        # sits at most one tick past its TTL, not worth a tighter schedule.
        self._scheduler.add_job(
            self._message_cleanup.tick,
            IntervalTrigger(seconds=TICK_SECONDS),
            id="message_cleanup",
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()
        log.info("poller started, tick every %ss", TICK_SECONDS)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _daily_history(self) -> None:
        """Once a day for everyone, on top of the per-session refresh (SPEC 5.4)."""
        for target in await self._repo.pollable_users():
            try:
                await self._fetcher.refresh_title_history(target.tg_id, target.xuid)
            except Exception:
                log.info("daily title history for tg_id=%s skipped", target.tg_id)
