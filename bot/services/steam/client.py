"""Steam Web API: resolving a profile and checking its visibility
(M-Steam-1, TODO.md — account linking only, no achievement polling yet).

Official, documented endpoints — unlike Xbox's hand-rolled contract-4
achievements or HowLongToBeat's reverse-engineered search, nothing here is
guesswork and nothing needs per-user OAuth. One API key for the whole bot;
the only thing that can go wrong per person is a private profile, which is
their own setting to fix, not ours.

Verified live against a real key: GetPlayerSummaries returned a real
profile with the fields this module reads, and ResolveVanityURL on a
deliberately made-up name came back as a clean "no match" rather than an
auth error — both calls genuinely reach Steam, not just parse correctly.
"""

from __future__ import annotations

import re

import httpx

BASE_URL = "https://api.steampowered.com"
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 10.0

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


async def _get(path: str, api_key: str, params: dict[str, str]) -> dict:
    query = {"key": api_key, "format": "json", **params}
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
