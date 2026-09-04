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
from bot.poller.cadence import debounce_passed, is_due, presence_interval
from bot.poller.fetcher import Fetcher
from bot.services.xbox.auth import NotConnectedError, TokenDeadError, TokenRefreshError
from bot.services.xbox.client import PresenceSnapshot, XboxApiError, XboxClient

log = logging.getLogger(__name__)


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

        # Presence returns an empty name for PC titles, so resolve it once when
        # the game starts rather than only when an achievement drops — the
        # admin card and the published message both read it from here.
        title_name = snapshot.title_name
        if snapshot.title_id and not title_name:
            title_name = await self._fetcher.ensure_title_name(
                target.tg_id, snapshot.title_id, None
            )

        await self._repo.save_presence_state(
            target.xuid,
            snapshot.state,
            snapshot.title_id,
            title_name,
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
                title_name,
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
        return debounce_passed(target.last_ach_poll_at, self._settings.achievement_poll_interval)

    def _is_due(self, target: PollTarget) -> bool:
        return is_due(target.updated_at, self._interval(target))

    def _interval(self, target: PollTarget) -> int:
        return presence_interval(
            online=target.state == "Online",
            in_game=bool(target.title_id),
            changed_at=target.changed_at,
            interval_in_game=self._settings.presence_interval_in_game,
            interval_online=self._settings.presence_interval_online,
            interval_offline=self._settings.presence_interval_offline,
            interval_idle=self._settings.presence_interval_idle,
        )

    async def _gamertag(self, tg_id: int) -> str:
        user = await self._repo.get_user(tg_id)
        return (user.gamertag if user and user.gamertag else None) or "Игрок"
