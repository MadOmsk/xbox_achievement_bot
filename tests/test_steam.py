"""Steam account linking (M-Steam-1, TODO.md) — URL/ID parsing and the
platform_links repo round-trip. The vanity-resolution and profile-fetch
network calls are mocked at the `_get` boundary; nothing here has ever hit
a real Steam API key (none configured while this was written)."""

from __future__ import annotations

import pytest

from bot.db.repo import Repo
from bot.services.steam import client as steam_client
from bot.services.steam.client import SteamApiError, get_profile, resolve_steam_id

STEAM_ID = "76561197960287930"  # a real, long-public Valve account (Gabe Newell)


async def test_resolve_steam_id_accepts_a_bare_id() -> None:
    assert await resolve_steam_id("key", STEAM_ID) == STEAM_ID


async def test_resolve_steam_id_extracts_from_a_profile_url() -> None:
    url = f"https://steamcommunity.com/profiles/{STEAM_ID}"
    assert await resolve_steam_id("key", url) == STEAM_ID


async def test_resolve_steam_id_extracts_from_a_profile_url_with_trailing_slash() -> None:
    url = f"https://steamcommunity.com/profiles/{STEAM_ID}/"
    assert await resolve_steam_id("key", url) == STEAM_ID


async def test_resolve_steam_id_looks_up_a_vanity_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(path: str, api_key: str, params: dict[str, str]) -> dict:
        assert path == "/ISteamUser/ResolveVanityURL/v1/"
        assert params == {"vanityurl": "gaben"}
        return {"success": 1, "steamid": STEAM_ID}

    monkeypatch.setattr(steam_client, "_get", fake_get)

    assert await resolve_steam_id("key", "https://steamcommunity.com/id/gaben") == STEAM_ID
    assert await resolve_steam_id("key", "gaben") == STEAM_ID  # bare vanity name, no URL at all


async def test_resolve_steam_id_raises_when_vanity_has_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(path: str, api_key: str, params: dict[str, str]) -> dict:
        return {"success": 42, "message": "No match"}

    monkeypatch.setattr(steam_client, "_get", fake_get)

    with pytest.raises(SteamApiError):
        await resolve_steam_id("key", "no-such-person-at-all")


async def test_get_profile_reads_persona_name_and_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(path: str, api_key: str, params: dict[str, str]) -> dict:
        assert path == "/ISteamUser/GetPlayerSummaries/v2/"
        return {
            "players": [{"steamid": STEAM_ID, "personaname": "Gabe", "communityvisibilitystate": 3}]
        }

    monkeypatch.setattr(steam_client, "_get", fake_get)

    profile = await get_profile("key", STEAM_ID)

    assert profile.steam_id == STEAM_ID
    assert profile.persona_name == "Gabe"
    assert profile.is_public is True


async def test_get_profile_flags_a_private_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(path: str, api_key: str, params: dict[str, str]) -> dict:
        return {
            "players": [
                {"steamid": STEAM_ID, "personaname": "Hiding", "communityvisibilitystate": 1}
            ]
        }

    monkeypatch.setattr(steam_client, "_get", fake_get)

    profile = await get_profile("key", STEAM_ID)

    assert profile.is_public is False


async def test_get_profile_raises_for_an_unknown_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(path: str, api_key: str, params: dict[str, str]) -> dict:
        return {"players": []}

    monkeypatch.setattr(steam_client, "_get", fake_get)

    with pytest.raises(SteamApiError):
        await get_profile("key", "0")


async def test_platform_link_round_trip(repo: Repo) -> None:
    await repo.ensure_user(1, "someone")
    assert await repo.get_platform_link(1, "steam") is None

    await repo.link_platform_account(1, "steam", STEAM_ID, "Gabe")
    link = await repo.get_platform_link(1, "steam")

    assert link is not None
    assert (link.platform, link.external_id, link.display_name) == ("steam", STEAM_ID, "Gabe")
    assert [platform_link.platform for platform_link in await repo.platform_links_of(1)] == [
        "steam"
    ]


async def test_relinking_replaces_the_previous_account(repo: Repo) -> None:
    await repo.ensure_user(1, "someone")
    await repo.link_platform_account(1, "steam", "111", "Old Name")
    await repo.link_platform_account(1, "steam", "222", "New Name")

    link = await repo.get_platform_link(1, "steam")

    assert link is not None
    assert (link.external_id, link.display_name) == ("222", "New Name")


async def test_unlink_removes_the_account(repo: Repo) -> None:
    await repo.ensure_user(1, "someone")
    await repo.link_platform_account(1, "steam", STEAM_ID, "Gabe")

    await repo.unlink_platform_account(1, "steam")

    assert await repo.get_platform_link(1, "steam") is None
