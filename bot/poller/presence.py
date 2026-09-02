"""Presence polling and the events it produces (SPEC 5.2, 5.3).

One tick a minute walks every connected user and decides whether it is time to
ask about him. A change of game or going offline is an event: it triggers the
final achievement request of the session and a title history refresh.

Every user is handled in isolation — one private profile or one expired token
must never stop the tick for everyone else.
"""

from __future__ import annotations

import logging

from bot.config import Settings
from bot.db.repo import PollTarget, Repo
from bot.poller.fetcher import Fetcher
from bot.services.xbox.auth import NotConnectedError, TokenDeadError, TokenRefreshError
from bot.services.xbox.client import PresenceSnapshot, XboxApiError, XboxClient
from bot.util import parse_iso, utcnow

log = logging.getLogger(__name__)

IDLE_AFTER_SECONDS = 2 * 3600

# The tick fires every 60s, but a timestamp is written a moment after the tick
# begins, so the next tick measures 59.x seconds and decides it is too early.
# Every interval would then silently double: presence once in two minutes,
# achievements once in four. The tolerance must be smaller than the tick.
DUE_TOLERANCE_SECONDS = 5


class PresencePoller:
    def __init__(
        self, settings: Settings, repo: Repo, client: XboxClient, fetcher: Fetcher
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._client = client
        self._fetcher = fetcher

    async def tick(self) -> None:
        for target in await self._repo.pollable_users():
            if not self._is_due(target):
                continue
            try:
                await self._handle(target)
            except (TokenDeadError, NotConnectedError) as exc:
                # The user is told about it by the reminder job, not from here.
                log.info("skipping tg_id=%s: %s", target.tg_id, exc)
            except (TokenRefreshError, XboxApiError) as exc:
                log.info("tg_id=%s not polled this tick: %s", target.tg_id, exc)
            except Exception:
                log.exception("unexpected failure while polling tg_id=%s", target.tg_id)

    async def _handle(self, target: PollTarget) -> None:
        snapshot = await self._client.presence(target.tg_id)
        changed = snapshot.state != target.state or snapshot.title_id != target.title_id

        await self._repo.save_presence_state(
            target.xuid,
            snapshot.state,
            snapshot.title_id,
            snapshot.title_name,
            changed=changed,
        )
        if snapshot.state == "Online":
            await self._repo.touch_last_online(target.tg_id)

        gamertag = await self._gamertag(target.tg_id)

        if changed and target.title_id:
            # The final request of the session: the last achievement is often
            # unlocked right before quitting (SPEC 5.3).
            await self._poll_achievements(
                target, gamertag, target.title_id, target.title_name, force=True
            )
            await self._fetcher.refresh_title_history(target.tg_id, target.xuid)

        if snapshot.in_game and snapshot.title_id:
            await self._poll_achievements(
                target,
                gamertag,
                snapshot.title_id,
                snapshot.title_name,
                force=changed,
                platform_hint=snapshot,
            )

    async def _poll_achievements(
        self,
        target: PollTarget,
        gamertag: str,
        title_id: str,
        title_name: str | None,
        *,
        force: bool,
        platform_hint: PresenceSnapshot | None = None,
    ) -> None:
        if not force and not self._debounce_passed(target):
            return
        platform = platform_hint.platform if platform_hint else "modern"
        await self._fetcher.poll_title(
            target.tg_id, target.xuid, gamertag, title_id, platform, title_name
        )

    def _debounce_passed(self, target: PollTarget) -> bool:
        """No more than one achievement request per game every two minutes."""
        last = parse_iso(target.last_ach_poll_at)
        if last is None:
            return True
        elapsed = (utcnow() - last).total_seconds()
        return elapsed >= self._settings.achievement_poll_interval - DUE_TOLERANCE_SECONDS

    def _is_due(self, target: PollTarget) -> bool:
        last = parse_iso(target.updated_at)
        if last is None:
            return True
        return (utcnow() - last).total_seconds() >= self._interval(target) - DUE_TOLERANCE_SECONDS

    def _interval(self, target: PollTarget) -> int:
        """Sparser polling of absent people is politeness, not thrift: the
        Microsoft budget is nowhere near a constraint (SPEC 5.2)."""
        if target.state == "Online":
            return (
                self._settings.presence_interval_in_game
                if target.title_id
                else self._settings.presence_interval_online
            )
        changed = parse_iso(target.changed_at)
        offline_for = (utcnow() - changed).total_seconds() if changed else 0.0
        if offline_for >= IDLE_AFTER_SECONDS:
            return self._settings.presence_interval_idle
        return self._settings.presence_interval_offline

    async def _gamertag(self, tg_id: int) -> str:
        user = await self._repo.get_user(tg_id)
        return (user.gamertag if user and user.gamertag else None) or "Игрок"
