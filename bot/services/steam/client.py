"""Steam Web API: profile linking (M-Steam-1) and achievement lookups
(SPEC 9, M-Steam-2b) — a stateless client only, no `repo`. Caching and
stitching schema/rarity onto a person's unlocked achievements happens one
layer up, in services/steam/achievements.py.

Official, documented endpoints — unlike Xbox's hand-rolled contract-4
achievements or HowLongToBeat's reverse-engineered search, nothing here is
guesswork and nothing needs per-user OAuth. One API key for the whole bot;
the only thing that can go wrong per person is a private profile, which is
their own setting to fix, not ours.

Verified live against a real key and a real linked account (appid 550 —
Left 4 Dead 2, 101 achievements): every function here has been run against
the genuine endpoint, not just parsed against a guessed shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from bot.services.rate_limiter import RateLimiter

BASE_URL = "https://api.steampowered.com"
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 10.0

# Valve's own documented cap is the only official number: 100,000 calls a
# day per key, no published per-second limit (2026-09-05, admin panel
# diagnostic — mirrors xbox/client.py's own RateLimiter, moved to a shared
# module for exactly this). The bot's actual traffic sits nowhere near this
# even in the busiest realistic case (batched presence + debounced
# achievement polling for ~30 people) — same "guard against a bug, not a
# real budget" reasoning as Xbox's own window. No process-wide lock is
# needed beyond RateLimiter's own: every Steam call in this bot already
# goes through this one module-level function.
RATE_WINDOWS: tuple[tuple[int, float], ...] = ((100_000, 86400.0),)
_limiter = RateLimiter(RATE_WINDOWS)


def rate_limit_usage() -> list[tuple[int, int, float]]:
    """(used, limit, window_seconds) — the admin panel's Steam counterpart
    to XboxClient.rate_limit_usage()."""
    return _limiter.usage()

# communityvisibilitystate: 1 = private, 2 = friends only, 3 = public.
_PUBLIC = 3

# A SteamID64 is always 17 digits. A profile URL carries one directly; a
# vanity URL (or a bare vanity name, typed without the URL around it) needs
# an extra lookup to turn into one.
_STEAM_ID_RE = re.compile(r"^\d{17}$")
_PROFILE_URL_RE = re.compile(r"steamcommunity\.com/profiles/(\d{17})")
_VANITY_URL_RE = re.compile(r"steamcommunity\.com/id/([^/\s]+)")


class SteamApiError(Exception):
    """Expected failure — unreachable API, bad key, or an unresolvable
    profile. Same "log it, tell the user, never crash" treatment as
    XboxApiError (SPEC 1.5)."""


class SteamProfile:
    __slots__ = ("is_public", "persona_name", "steam_id")

    def __init__(self, steam_id: str, persona_name: str, is_public: bool) -> None:
        self.steam_id = steam_id
        self.persona_name = persona_name
        self.is_public = is_public


@dataclass(slots=True)
class RawAchievement:
    """One entry from GetPlayerAchievements — already localized (`l=russian`
    on the request), unlike Xbox's separate localization call (SPEC 9,
    M-Steam-2b)."""

    apiname: str
    achieved: bool
    unlocktime: int  # unix epoch; 0 means "no real date", same class of
    # placeholder as Xbox's 0001-01-01/1753-01-01 (parsed in achievements.py)
    name: str
    description: str | None


@dataclass(slots=True)
class RawSchemaAchievement:
    """One entry from GetSchemaForGame — about the game, not the person, so
    cached forever like hltb_cache (SPEC 9, M-Steam-2b). Steam calls the
    identifier `name` here and `apiname` in GetPlayerAchievements above —
    normalized to `apiname` on this side so both endpoints stitch on the
    same field name."""

    apiname: str
    icon: str | None  # unlocked icon, not `icongray` — only unlocked ever gets published
    hidden: bool  # Steam's own secrecy flag, Steam's isSecret equivalent (7.1)


async def resolve_steam_id(api_key: str, raw: str) -> str:
    """Whatever a person would actually paste from their own profile page:
    a bare SteamID64, a `.../profiles/<id>` link, a `.../id/<vanity>` link,
    or just the vanity name on its own."""
    raw = raw.strip()

    if match := _PROFILE_URL_RE.search(raw):
        return match.group(1)
    if _STEAM_ID_RE.match(raw):
        return raw

    vanity_match = _VANITY_URL_RE.search(raw)
    vanity = vanity_match.group(1) if vanity_match else raw
    return await _resolve_vanity(api_key, vanity)


async def _resolve_vanity(api_key: str, vanity: str) -> str:
    payload = await _get("/ISteamUser/ResolveVanityURL/v1/", api_key, {"vanityurl": vanity})
    if payload.get("success") != 1 or not payload.get("steamid"):
        raise SteamApiError(f"Steam has no profile named {vanity!r}")
    return str(payload["steamid"])


async def get_profile(api_key: str, steam_id: str) -> SteamProfile:
    payload = await _get("/ISteamUser/GetPlayerSummaries/v2/", api_key, {"steamids": steam_id})
    players = payload.get("players") or []
    if not players:
        raise SteamApiError(f"Steam has no profile for id={steam_id}")
    player = players[0]
    return SteamProfile(
        steam_id=str(player.get("steamid", steam_id)),
        persona_name=player.get("personaname") or steam_id,
        is_public=player.get("communityvisibilitystate") == _PUBLIC,
    )


@dataclass(slots=True)
class SteamPresence:
    """One GetPlayerSummaries entry, presence fields only (SPEC 9,
    M-Steam-2c) — persona_name is included too since a batched presence
    call already has it fresh, sparing the poller a separate lookup the
    way Xbox needs one for a PC title's name (presence.py)."""

    steam_id: str
    persona_name: str
    persona_state: int  # Steam's own enum, 0=offline..6
    gameid: str | None
    game_name: str | None  # gameextrainfo — set only while actually playing


async def get_presence_batch(api_key: str, steam_ids: list[str]) -> dict[str, SteamPresence]:
    """Up to 100 SteamIDs in one official call (SPEC 9, M-Steam-2c) — the
    caller is responsible for chunking a longer list. A profile Steam
    doesn't return at all (deleted/banned account) is simply absent from
    the result, not an error."""
    payload = await _get(
        "/ISteamUser/GetPlayerSummaries/v2/", api_key, {"steamids": ",".join(steam_ids)}
    )
    result: dict[str, SteamPresence] = {}
    for player in payload.get("players") or []:
        steam_id = player.get("steamid")
        if not steam_id:
            continue
        result[str(steam_id)] = SteamPresence(
            steam_id=str(steam_id),
            persona_name=player.get("personaname") or str(steam_id),
            persona_state=int(player.get("personastate") or 0),
            gameid=player.get("gameid"),
            game_name=player.get("gameextrainfo"),
        )
    return result


@dataclass(slots=True)
class OwnedGame:
    appid: str
    name: str
    playtime_forever: int


async def get_owned_games(api_key: str, steam_id: str) -> list[OwnedGame]:
    """Only games with real playtime (SPEC 9, M-Steam-2d) — a game never
    launched has nothing to backfill, and asking about it wastes a request
    for every game in a large library. Verified live: 617 owned games, 306
    with playtime_forever > 0, on the same account used throughout M-Steam
    research."""
    payload = await _get(
        "/IPlayerService/GetOwnedGames/v1/",
        api_key,
        {"steamid": steam_id, "include_appinfo": "1"},
    )
    games = payload.get("games") or []
    return [
        OwnedGame(
            appid=str(item["appid"]),
            name=item.get("name") or str(item["appid"]),
            playtime_forever=int(item.get("playtime_forever") or 0),
        )
        for item in games
        if item.get("appid") and int(item.get("playtime_forever") or 0) > 0
    ]


async def get_player_achievements(
    api_key: str, steam_id: str, appid: str
) -> list[RawAchievement]:
    """`success: false` in the body is an expected response, not an HTTP
    error (SPEC 9, M-Steam-2b) — a profile that went private after linking,
    or a game with no achievement stats at all. Both just mean "nothing for
    this game right now", same as an empty list."""
    payload = await _get(
        "/ISteamUserStats/GetPlayerAchievements/v1/",
        api_key,
        {"steamid": steam_id, "appid": appid, "l": "russian"},
    )
    stats = payload.get("playerstats") or {}
    if not stats.get("success"):
        return []
    return [
        RawAchievement(
            apiname=str(item["apiname"]),
            achieved=bool(item.get("achieved")),
            unlocktime=int(item.get("unlocktime") or 0),
            name=item.get("name") or item["apiname"],
            description=item.get("description"),
        )
        for item in stats.get("achievements") or []
        if item.get("apiname")
    ]


async def get_schema(api_key: str, appid: str) -> list[RawSchemaAchievement]:
    """About the game, not any one person — cache this forever, never per
    request (SPEC 9, M-Steam-2b)."""
    payload = await _get(
        "/ISteamUserStats/GetSchemaForGame/v2/", api_key, {"appid": appid, "l": "russian"}
    )
    achievements = (payload.get("game") or {}).get("availableGameStats", {}).get(
        "achievements"
    ) or []
    return [
        RawSchemaAchievement(
            apiname=str(item["name"]),  # Steam's own key here, not `apiname`
            icon=item.get("icon") or None,
            hidden=bool(item.get("hidden")),
        )
        for item in achievements
        if item.get("name")
    ]


async def get_global_percentages(appid: str) -> dict[str, float]:
    """No API key at all — this endpoint is public (SPEC 9, M-Steam-2b).
    Real percentages drift over time, unlike the schema above, so the
    caching layer (services/steam/achievements.py) expires this one."""
    payload = await _get(
        "/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/", "", {"gameid": appid}
    )
    achievements = (payload.get("achievementpercentages") or {}).get("achievements") or []
    result: dict[str, float] = {}
    for item in achievements:
        name = item.get("name")
        if not name:
            continue
        try:
            result[str(name)] = float(item.get("percent", 0))
        except (TypeError, ValueError):
            continue
    return result


async def _get(path: str, api_key: str, params: dict[str, str]) -> dict:
    # GetGlobalAchievementPercentagesForApp needs no key at all (SPEC 9,
    # M-Steam-2b) — omitted rather than sent empty, matching what was
    # verified live against the real endpoint.
    query = {"format": "json", **params}
    if api_key:
        query["key"] = api_key
    await _limiter.acquire()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.get(BASE_URL + path, params=query)
            except httpx.RequestError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise SteamApiError(f"Steam request failed: {exc}") from None
                continue

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    raise SteamApiError("Steam response is not JSON") from None
                result = data.get("response", data)
                return result if isinstance(result, dict) else {}

            if response.status_code in (401, 403):
                raise SteamApiError("Steam rejected the API key")
            if attempt == MAX_ATTEMPTS:
                raise SteamApiError(f"Steam returned {response.status_code}")

    raise SteamApiError("Steam request gave up")  # pragma: no cover — loop always returns/raises
