"""services/steam/achievements.py: stitching player achievements onto
schema/rarity, and the two cache tables behind it (SPEC 9, M-Steam-2b).
Steam API calls are mocked at the client-function boundary — nothing here
has ever hit a real Steam API key."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.db.repo import Repo, SteamSchemaAchievement
from bot.services.steam import achievements as steam_achievements
from bot.services.steam.achievements import _parse_unlocktime, fetch_unlocked
from bot.services.steam.client import RawAchievement, RawSchemaAchievement

STEAM_ID = "76561197960287930"
APPID = "550"


def _raw(apiname: str, *, achieved: bool, unlocktime: int = 0, name: str = "") -> RawAchievement:
    return RawAchievement(
        apiname=apiname,
        achieved=achieved,
        unlocktime=unlocktime,
        name=name or apiname,
        description=f"{apiname} description",
    )


def test_parse_unlocktime_treats_zero_as_no_date() -> None:
    assert _parse_unlocktime(0) is None


def test_parse_unlocktime_converts_a_real_unix_timestamp() -> None:
    assert _parse_unlocktime(1260104110) == datetime.fromtimestamp(1260104110, tz=UTC)


async def test_fetch_unlocked_skips_locked_achievements(
    repo: Repo, monkeypatch
) -> None:
    async def fake_player(api_key: str, steam_id: str, appid: str) -> list[RawAchievement]:
        return [_raw("ACH_LOCKED", achieved=False), _raw("ACH_A", achieved=True)]

    async def fake_schema(api_key: str, appid: str) -> list[RawSchemaAchievement]:
        return [RawSchemaAchievement(apiname="ACH_A", icon="icon.jpg", hidden=False)]

    async def fake_percentages(appid: str) -> dict[str, float]:
        return {"ACH_A": 42.0}

    monkeypatch.setattr(steam_achievements, "get_player_achievements", fake_player)
    monkeypatch.setattr(steam_achievements, "get_schema", fake_schema)
    monkeypatch.setattr(steam_achievements, "get_global_percentages", fake_percentages)

    result = await fetch_unlocked(repo, "key", STEAM_ID, APPID)

    assert [a.achievement_id for a in result] == ["ACH_A"]
    assert result[0].platform == "steam"
    assert result[0].gamerscore == 0
    assert result[0].rarity_percent == 42.0
    assert result[0].icon_url == "icon.jpg"
    assert result[0].is_secret is False


async def test_fetch_unlocked_marks_hidden_achievements_secret(repo: Repo, monkeypatch) -> None:
    async def fake_player(api_key: str, steam_id: str, appid: str) -> list[RawAchievement]:
        return [_raw("ACH_SECRET", achieved=True)]

    async def fake_schema(api_key: str, appid: str) -> list[RawSchemaAchievement]:
        return [RawSchemaAchievement(apiname="ACH_SECRET", icon="s.jpg", hidden=True)]

    async def fake_percentages(appid: str) -> dict[str, float]:
        return {}

    monkeypatch.setattr(steam_achievements, "get_player_achievements", fake_player)
    monkeypatch.setattr(steam_achievements, "get_schema", fake_schema)
    monkeypatch.setattr(steam_achievements, "get_global_percentages", fake_percentages)

    result = await fetch_unlocked(repo, "key", STEAM_ID, APPID)

    assert result[0].is_secret is True
    assert result[0].rarity_percent is None  # not in the percentages map at all


async def test_fetch_unlocked_returns_nothing_without_calling_schema_or_rarity(
    repo: Repo, monkeypatch
) -> None:
    """Nothing achieved (or a private profile, already flattened to [] by
    client.py) — no point spending a schema/rarity request on a game with
    nothing to publish."""
    calls = {"schema": 0, "rarity": 0}

    async def fake_player(api_key: str, steam_id: str, appid: str) -> list[RawAchievement]:
        return [_raw("ACH_LOCKED", achieved=False)]

    async def fake_schema(api_key: str, appid: str) -> list[RawSchemaAchievement]:
        calls["schema"] += 1
        return []

    async def fake_percentages(appid: str) -> dict[str, float]:
        calls["rarity"] += 1
        return {}

    monkeypatch.setattr(steam_achievements, "get_player_achievements", fake_player)
    monkeypatch.setattr(steam_achievements, "get_schema", fake_schema)
    monkeypatch.setattr(steam_achievements, "get_global_percentages", fake_percentages)

    assert await fetch_unlocked(repo, "key", STEAM_ID, APPID) == []
    assert calls == {"schema": 0, "rarity": 0}


async def test_schema_cache_round_trip(repo: Repo) -> None:
    assert await repo.steam_schema_get_cached(APPID) is None

    achievements = [SteamSchemaAchievement(apiname="ACH_A", icon="a.jpg", hidden=False)]
    await repo.steam_schema_cache_result(APPID, "Left 4 Dead 2", achievements)

    cached = await repo.steam_schema_get_cached(APPID)
    assert cached is not None
    game_name, cached_achievements = cached
    assert game_name == "Left 4 Dead 2"
    assert cached_achievements == achievements


async def test_schema_is_read_from_cache_not_refetched(repo: Repo, monkeypatch) -> None:
    calls = {"schema": 0}

    async def fake_schema(api_key: str, appid: str) -> list[RawSchemaAchievement]:
        calls["schema"] += 1
        return [RawSchemaAchievement(apiname="ACH_A", icon="a.jpg", hidden=False)]

    async def fake_player(api_key: str, steam_id: str, appid: str) -> list[RawAchievement]:
        return [_raw("ACH_A", achieved=True)]

    async def fake_percentages(appid: str) -> dict[str, float]:
        return {}

    monkeypatch.setattr(steam_achievements, "get_player_achievements", fake_player)
    monkeypatch.setattr(steam_achievements, "get_schema", fake_schema)
    monkeypatch.setattr(steam_achievements, "get_global_percentages", fake_percentages)

    await fetch_unlocked(repo, "key", STEAM_ID, APPID)
    await fetch_unlocked(repo, "key", STEAM_ID, APPID)

    assert calls["schema"] == 1  # second call served entirely from the cache


async def test_rarity_cache_round_trip(repo: Repo) -> None:
    assert await repo.steam_rarity_get_cached(APPID) is None

    await repo.steam_rarity_cache_result(APPID, {"ACH_A": 42.0})

    cached = await repo.steam_rarity_get_cached(APPID)
    assert cached is not None
    percentages, cached_at = cached
    assert percentages == {"ACH_A": 42.0}
    assert cached_at  # a real timestamp was stamped


async def test_rarity_is_refetched_once_stale(repo: Repo, monkeypatch) -> None:
    # A cached_at older than the TTL (SPEC 9, M-Steam-2b: "раз в неделю") must
    # not be served as-is — real percentages drift, unlike the schema cache.
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await repo._conn.execute(
        "INSERT INTO steam_rarity_cache (appid, percentages, cached_at) VALUES (?, ?, ?)",
        (APPID, '{"ACH_A": 10.0}', stale),
    )
    await repo._conn.commit()

    calls = {"rarity": 0}

    async def fake_percentages(appid: str) -> dict[str, float]:
        calls["rarity"] += 1
        return {"ACH_A": 99.0}

    async def fake_player(api_key: str, steam_id: str, appid: str) -> list[RawAchievement]:
        return [_raw("ACH_A", achieved=True)]

    async def fake_schema(api_key: str, appid: str) -> list[RawSchemaAchievement]:
        return []

    monkeypatch.setattr(steam_achievements, "get_player_achievements", fake_player)
    monkeypatch.setattr(steam_achievements, "get_schema", fake_schema)
    monkeypatch.setattr(steam_achievements, "get_global_percentages", fake_percentages)

    result = await fetch_unlocked(repo, "key", STEAM_ID, APPID)

    assert calls["rarity"] == 1
    assert result[0].rarity_percent == 99.0


async def test_rarity_within_ttl_is_not_refetched(repo: Repo, monkeypatch) -> None:
    await repo.steam_rarity_cache_result(APPID, {"ACH_A": 10.0})

    calls = {"rarity": 0}

    async def fake_percentages(appid: str) -> dict[str, float]:
        calls["rarity"] += 1
        return {"ACH_A": 99.0}

    async def fake_player(api_key: str, steam_id: str, appid: str) -> list[RawAchievement]:
        return [_raw("ACH_A", achieved=True)]

    async def fake_schema(api_key: str, appid: str) -> list[RawSchemaAchievement]:
        return []

    monkeypatch.setattr(steam_achievements, "get_player_achievements", fake_player)
    monkeypatch.setattr(steam_achievements, "get_schema", fake_schema)
    monkeypatch.setattr(steam_achievements, "get_global_percentages", fake_percentages)

    result = await fetch_unlocked(repo, "key", STEAM_ID, APPID)

    assert calls["rarity"] == 0
    assert result[0].rarity_percent == 10.0
