"""Xbox Live requests: presence, achievements, title history (SPEC 4).

The library covers presence and titlehub. Achievements we do ourselves, because
`rarity` only exists on contract 4 and the library sends contract 1 or 2 — the
whole rarity feature depends on this one request being hand-written.

Nothing here knows about Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import httpx
from xbox.webapi.api.client import XboxLiveClient

from bot.services.xbox.auth import XboxAuthService
from bot.services.xbox.models import (
    ParsedAchievement,
    Platform,
    continuation_token,
    parse_achievements,
)

log = logging.getLogger(__name__)

ACHIEVEMENTS_URL = "https://achievements.xboxlive.com/users/xuid({xuid})/achievements"
PAGE_SIZE = 1000
MAX_ATTEMPTS = 3

# Microsoft's documented windows for the achievements service. We use about 1%
# of this, so the limiter is a guard against a bug in the poller, not a budget.
RATE_WINDOWS: tuple[tuple[int, float], ...] = ((100, 15.0), (300, 300.0))

X360_DEVICES = {"Xbox360", "Xbox 360"}


class XboxApiError(Exception):
    """Expected failure — the poller logs it and moves to the next user."""


class ProfileUnavailableError(XboxApiError):
    """Private profile, or the account cannot be read with this token."""


@dataclass(slots=True)
class PresenceSnapshot:
    state: str
    title_id: str | None
    title_name: str | None
    platform: Platform
    last_seen_at: datetime | None

    @property
    def in_game(self) -> bool:
        return self.state == "Online" and self.title_id is not None


@dataclass(slots=True)
class TitleHistoryEntry:
    title_id: str
    name: str
    platform: Platform
    current_gamerscore: int | None
    max_gamerscore: int | None
    achievements_unlocked: int | None
    achievements_total: int | None
    last_played_at: str | None


class RateLimiter:
    """Sliding windows, shared by every user of the bot."""

    def __init__(self, windows: tuple[tuple[int, float], ...] = RATE_WINDOWS) -> None:
        self._windows = [(limit, span, deque[float]()) for limit, span in windows]
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                wait = 0.0
                for limit, span, calls in self._windows:
                    while calls and now - calls[0] > span:
                        calls.popleft()
                    if len(calls) >= limit:
                        wait = max(wait, span - (now - calls[0]))
                if wait <= 0:
                    for _, _, calls in self._windows:
                        calls.append(now)
                    return
            log.debug("rate limiter sleeping for %.1fs", wait)
            await asyncio.sleep(wait)

    def usage(self) -> list[tuple[int, int, float]]:
        """A snapshot of (used, limit, window_seconds) per window, for the
        admin panel's API diagnostic (SPEC 6.4) — a read, not an acquire, so
        checking it never itself counts against the budget."""
        now = asyncio.get_running_loop().time()
        result = []
        for limit, span, calls in self._windows:
            while calls and now - calls[0] > span:
                calls.popleft()
            result.append((len(calls), limit, span))
        return result


class XboxClient:
    def __init__(self, auth: XboxAuthService, limiter: RateLimiter | None = None) -> None:
        self._auth = auth
        self._limiter = limiter or RateLimiter()

    def rate_limit_usage(self) -> list[tuple[int, int, float]]:
        """(used, limit, window_seconds) for each achievements-service window
        this client shares across every user (SPEC 4, 6.4)."""
        return self._limiter.usage()

    # ----------------------------------------------------------- presence

    async def presence(self, tg_id: int) -> PresenceSnapshot:
        """Ask a user about himself with his own token (SPEC 5.2).

        No batching and no friendship with a bot account: everyone can always
        see himself, whatever his privacy settings are.
        """
        manager = await self._auth.authenticated_manager(tg_id)
        client = XboxLiveClient(manager)
        await self._limiter.acquire()
        try:
            item = await client.presence.get_presence_own()
        except httpx.HTTPStatusError as exc:
            raise _translate(exc) from None
        except httpx.RequestError as exc:
            raise XboxApiError(f"presence request failed: {exc}") from None

        title_id, title_name, device = _current_title(item)
        last_seen = getattr(item, "last_seen", None)
        if device is None and last_seen is not None:
            device = last_seen.device_type
        return PresenceSnapshot(
            state=item.state or "Offline",
            title_id=title_id,
            title_name=title_name,
            platform="x360" if device in X360_DEVICES else "modern",
            last_seen_at=getattr(last_seen, "timestamp", None),
        )

    # -------------------------------------------------------- achievements

    async def title_achievements(
        self, tg_id: int, title_id: str, platform: Platform
    ) -> list[ParsedAchievement]:
        """Achievements of one game — the only request that carries rarity.

        `platform` is a hint from presence, and presence reports the *console*,
        not the game: an Xbox 360 title played through back-compat on a Series X
        arrives here as "modern". Contract 4 answers such a title with an empty
        list, while a modern title always returns its full set (including
        NotStarted), so an empty answer means "wrong contract", not "no
        achievements" — and we ask again as Xbox 360. Without this a whole
        back-compat session would be published as nothing at all.
        """
        params = {"titleId": title_id, "maxItems": str(PAGE_SIZE)}
        if platform == "x360":
            return parse_achievements(
                await self._get_achievements(tg_id, "1", params), "x360", title_id
            )

        payload = await self._get_achievements(tg_id, "4", params)
        if payload.get("achievements"):
            return parse_achievements(payload, "modern", title_id)

        log.info("title %s looks like Xbox 360, retrying on contract 1", title_id)
        return parse_achievements(
            await self._get_achievements(tg_id, "1", params), "x360", title_id
        )

    async def all_achievements(self, tg_id: int) -> list[ParsedAchievement]:
        """Every achievement of the player, for backfill only (SPEC 5.6).

        Contract 2, no titleId: rarity is missing here, and that is fine —
        these rows are never published, they only mark "already seen".
        """
        collected: list[ParsedAchievement] = []
        params = {"maxItems": str(PAGE_SIZE)}
        for _ in range(100):  # a hard stop; nobody has 100k achievements
            payload = await self._get_achievements(tg_id, "2", params)
            collected.extend(parse_achievements(payload, "modern"))
            token = continuation_token(payload)
            if not token:
                break
            params = {"maxItems": str(PAGE_SIZE), "continuationToken": token}
        return collected

    async def _get_achievements(self, tg_id: int, contract: str, params: dict[str, str]) -> dict:
        manager = await self._auth.authenticated_manager(tg_id)
        assert manager.xsts_token is not None
        url = ACHIEVEMENTS_URL.format(xuid=manager.xsts_token.xuid)
        headers = {
            "Authorization": manager.xsts_token.authorization_header_value,
            "x-xbl-contract-version": contract,
            "Accept": "application/json",
            "Accept-Language": "en-US",
        }

        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._limiter.acquire()
            try:
                response = await manager.session.get(url, params=params, headers=headers)
            except httpx.RequestError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise XboxApiError(f"achievements request failed: {exc}") from None
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 429:
                # Respect Retry-After when Microsoft bothers to send it.
                delay = _retry_after(response) or 2**attempt
                if attempt == MAX_ATTEMPTS:
                    raise XboxApiError("achievements rate limited")
                log.info("rate limited by Xbox Live, sleeping %.0fs", delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code in (401, 403, 404):
                raise ProfileUnavailableError(f"achievements unavailable: {response.status_code}")
            if response.status_code >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise XboxApiError(f"Xbox Live returned {response.status_code}")
                await asyncio.sleep(2**attempt)
                continue

            try:
                payload = response.json()
            except ValueError:
                raise XboxApiError("achievements response is not JSON") from None
            return payload if isinstance(payload, dict) else {}

        raise XboxApiError("achievements request gave up")

    async def gamerscore(self, tg_id: int) -> int | None:
        """The real total from the profile.

        Summing title history would understate it: the history request is
        capped, and an account with more titles than the cap silently loses the
        rest of its score.
        """
        manager = await self._auth.authenticated_manager(tg_id)
        assert manager.xsts_token is not None
        client = XboxLiveClient(manager)
        await self._limiter.acquire()
        try:
            response = await client.profile.get_profile_by_xuid(manager.xsts_token.xuid)
        except httpx.HTTPStatusError as exc:
            raise _translate(exc) from None
        except httpx.RequestError as exc:
            raise XboxApiError(f"profile request failed: {exc}") from None

        for user in getattr(response, "profile_users", None) or []:
            for setting in getattr(user, "settings", None) or []:
                if getattr(setting, "id", None) == "Gamerscore":
                    try:
                        return int(setting.value)
                    except (TypeError, ValueError):
                        return None
        return None

    async def resolve_title(self, tg_id: int, title_id: str) -> TitleHistoryEntry | None:
        """Look one game up by id.

        Presence returns an empty name for PC titles (seen live on
        WindowsOneCore), and a message saying "неизвестная игра" is worse than
        one extra request per new game — the answer is cached in `titles`.
        """
        manager = await self._auth.authenticated_manager(tg_id)
        client = XboxLiveClient(manager)
        await self._limiter.acquire()
        try:
            response = await client.titlehub.get_title_info(title_id)
        except httpx.HTTPStatusError as exc:
            raise _translate(exc) from None
        except httpx.RequestError as exc:
            raise XboxApiError(f"title info request failed: {exc}") from None

        for title in response.titles or []:
            return _as_entry(title)
        return None

    # -------------------------------------------------------- title history

    async def title_history(self, tg_id: int, max_items: int = 200) -> list[TitleHistoryEntry]:
        """Source of /stats and /online, of the x360 pass in backfill(), and of
        the "recent games" table.

        Not the source of the headline gamerscore anywhere — that is always
        `users.gamerscore` from the profile (SPEC 5.4), never a sum over this.
        The only thing this cap actually gates is backfill's x360 achievement
        sweep, and that only needs to cover what "За месяц" cares about: a
        person does not play 200+ distinct titles in 30 days, so 200 is not a
        corner cut, it is already generous. (A live account with 1091 titles
        briefly ran with max_items=2000 while chasing what looked like a gap
        in "Всего" — that turned out to be the wrong target, since "Всего" is
        a count, not a score; reverted once that was clear.)
        """
        manager = await self._auth.authenticated_manager(tg_id)
        assert manager.xsts_token is not None
        client = XboxLiveClient(manager)
        await self._limiter.acquire()
        try:
            response = await client.titlehub.get_title_history(
                manager.xsts_token.xuid, max_items=max_items
            )
        except httpx.HTTPStatusError as exc:
            raise _translate(exc) from None
        except httpx.RequestError as exc:
            raise XboxApiError(f"title history request failed: {exc}") from None

        return [_as_entry(title) for title in response.titles or []]


def _as_entry(title: object) -> TitleHistoryEntry:
    achievement = getattr(title, "achievement", None)
    history = getattr(title, "title_history", None)
    devices = getattr(title, "devices", None) or []
    return TitleHistoryEntry(
        title_id=str(title.title_id),
        name=title.name or "",
        platform="x360" if any(d in X360_DEVICES for d in devices) else "modern",
        current_gamerscore=getattr(achievement, "current_gamerscore", None),
        max_gamerscore=getattr(achievement, "total_gamerscore", None),
        achievements_unlocked=getattr(achievement, "current_achievements", None),
        achievements_total=getattr(achievement, "total_achievements", None),
        last_played_at=_as_iso(getattr(history, "last_time_played", None)),
    )


def _current_title(item: object) -> tuple[str | None, str | None, str | None]:
    """The game a person is actually playing, not the dashboard behind it."""
    for device in getattr(item, "devices", None) or []:
        for title in getattr(device, "titles", None) or []:
            if getattr(title, "placement", None) != "Full":
                continue
            if getattr(title, "state", None) != "Active":
                continue
            name = getattr(title, "name", None)
            if name == "Home":  # the dashboard is not a game
                continue
            return str(title.id), name, getattr(device, "type", None)
    return None, None, None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _translate(exc: httpx.HTTPStatusError) -> XboxApiError:
    status = exc.response.status_code
    if status in (401, 403, 404):
        return ProfileUnavailableError(f"profile unavailable: {status}")
    return XboxApiError(f"Xbox Live returned {status}")


def _as_iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)
