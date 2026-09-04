"""Steam presence polling (SPEC 9, M-Steam-2c) — the Steam counterpart of
poller/presence.py, with one structural difference: GetPlayerSummaries
accepts up to 100 SteamIDs in a single official call, unlike Xbox's
one-account-per-request presence provider. So the tick collects who is due
exactly the same way (each target has its own `_is_due`/`_interval`), but
the HTTP call itself goes out in batches covering everyone due at once,
not one request per person.

Every target is handled in isolation — one batch failure or one person's
unexpected error must never stop the tick for everyone else, the same
discipline presence.py already follows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from bot.config import Settings
from bot.db.repo import Repo, SteamPollTarget
from bot.poller.cadence import debounce_passed, is_due, presence_interval
from bot.poller.steam_fetcher import SteamFetcher
from bot.services.steam.client import SteamApiError, SteamPresence, get_presence_batch
from bot.util import parse_iso, utcnow

log = logging.getLogger(__name__)

BATCH_SIZE = 100  # GetPlayerSummaries' own documented limit per call

# Found live: Steam's own presence (GetPlayerSummaries) sometimes stops
# reporting gameid for several minutes while someone keeps playing — an
# achievement sat unseen for ~19 minutes because of exactly this, achievement
# polling only runs for games the person is currently shown playing (SPEC
# 5.3's own reasoning: no gameid, no rarity data worth trusting either). As
# long as we saw them in a game within this window, keep polling that game
# through the gap rather than going idle until presence recovers on its own.
# 10 minutes: generous enough for the gaps seen so far, short enough that a
# genuine quit doesn't keep getting polled long after the fact.
GRACE_PERIOD_SECONDS = 10 * 60


class SteamPresencePoller:
    def __init__(self, settings: Settings, repo: Repo, fetcher: SteamFetcher) -> None:
        self._settings = settings
        self._repo = repo
        self._fetcher = fetcher

    async def tick(self) -> None:
        if self._settings.steam_api_key is None:
            return  # Steam not configured on this instance — same silent
            # skip /connect_steam already does (M-Steam-1)
        api_key = self._settings.steam_api_key.get_secret_value()

        due = [t for t in await self._repo.steam_pollable_users() if self._is_due(t)]
        for chunk in _chunks(due, BATCH_SIZE):
            try:
                snapshots = await get_presence_batch(api_key, [t.steam_id for t in chunk])
            except SteamApiError as exc:
                log.info("steam presence batch skipped: %s", exc)
                continue  # this chunk only — at most 100 people, next tick retries
            for target in chunk:
                snapshot = snapshots.get(target.steam_id)
                if snapshot is None:
                    continue  # private/deleted profile this tick — skip, not fatal
                try:
                    await self._handle(target, snapshot)
                except Exception:
                    log.exception("unexpected failure polling steam_id=%s", target.steam_id)

    async def _handle(self, target: SteamPollTarget, snapshot: SteamPresence) -> None:
        changed = (
            snapshot.persona_state != target.persona_state or snapshot.gameid != target.gameid
        )
        await self._repo.save_steam_presence_state(
            target.steam_id,
            snapshot.persona_state,
            snapshot.gameid,
            snapshot.game_name,
            changed=changed,
        )
        # Already have a fresh persona name from this same batch call — keep
        # the panel/connect card from drifting stale, at zero extra cost.
        await self._repo.update_platform_display_name(target.tg_id, "steam", snapshot.persona_name)

        in_game = snapshot.persona_state != 0 and snapshot.gameid is not None

        if changed and target.gameid:
            # The final request of the session: an unlock is often right
            # before quitting (same reasoning as presence.py, SPEC 5.3).
            await self._poll_achievements(
                target, snapshot.persona_name, target.gameid, target.game_name, force=True
            )

        if in_game:
            assert snapshot.gameid is not None
            await self._poll_achievements(
                target, snapshot.persona_name, snapshot.gameid, snapshot.game_name, force=changed
            )
        elif not changed:
            # `changed` alone already got a forced poll of target.gameid just
            # above (the "final" branch) — on the very first tick a game
            # goes missing, that already covers this exact game. Grace only
            # needs to pick up steady-state ticks after that, where nothing
            # "changed" but the gap hasn't closed yet.
            grace_game = self._grace_game(target, snapshot)
            if grace_game is not None:
                gameid, game_name = grace_game
                # Not "final" and not a fresh in_game reading — an ordinary
                # continuation of the same session, so it stays on the usual
                # debounce cadence rather than forcing every tick.
                await self._poll_achievements(
                    target, snapshot.persona_name, gameid, game_name, force=False
                )

    def _grace_game(
        self, target: SteamPollTarget, snapshot: SteamPresence
    ) -> tuple[str, str | None] | None:
        """Bridges a presence gap (GRACE_PERIOD_SECONDS, module docstring):
        gameid missing right now, but recently confirmed, and the person
        isn't reported fully offline — most likely still playing, Steam's
        own presence just hasn't said so this tick."""
        if snapshot.persona_state == 0:
            return None  # actually offline — no game to keep polling
        if target.last_active_gameid is None or target.last_active_at is None:
            return None
        last_active = parse_iso(target.last_active_at)
        if last_active is None:
            return None
        if (utcnow() - last_active).total_seconds() > GRACE_PERIOD_SECONDS:
            return None
        return target.last_active_gameid, target.last_active_game_name

    async def _poll_achievements(
        self,
        target: SteamPollTarget,
        persona_name: str,
        gameid: str,
        game_name: str | None,
        *,
        force: bool,
    ) -> None:
        if not force and not self._debounce_passed(target):
            return
        await self._fetcher.poll_title(
            target.tg_id, target.steam_id, persona_name, gameid, game_name
        )

    def _debounce_passed(self, target: SteamPollTarget) -> bool:
        # Reuses Xbox's own achievement_poll_interval rather than a separate
        # Steam-specific setting — the politeness reasoning is identical and
        # Steam's own budget is nowhere near a constraint either (SPEC 5.3).
        return debounce_passed(target.last_ach_poll_at, self._settings.achievement_poll_interval)

    def _is_due(self, target: SteamPollTarget) -> bool:
        return is_due(target.updated_at, self._interval(target))

    def _interval(self, target: SteamPollTarget) -> int:
        # Reuses the same presence_interval_* settings as Xbox rather than a
        # Steam-specific copy (SPEC 9, M-Steam-2c) — Steam's own budget isn't
        # the constraint here either.
        return presence_interval(
            online=target.persona_state is not None and target.persona_state != 0,
            in_game=bool(target.gameid),
            changed_at=target.changed_at,
            interval_in_game=self._settings.presence_interval_in_game,
            interval_online=self._settings.presence_interval_online,
            interval_offline=self._settings.presence_interval_offline,
            interval_idle=self._settings.presence_interval_idle,
        )


def _chunks(items: list[SteamPollTarget], size: int) -> Iterator[list[SteamPollTarget]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
