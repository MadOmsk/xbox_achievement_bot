"""poller/steam_fetcher.py: dedup, publish, and backfill isolation (SPEC 9,
M-Steam-2c/2d) — the Steam counterpart of test_poller.py's Fetcher tests.
fetch_unlocked()/get_owned_games() are faked at the module boundary, same
as the rest of this project's poller tests fake the Xbox client."""

from __future__ import annotations

from bot.db.repo import AchievementRow, Repo
from bot.poller import steam_fetcher as steam_fetcher_module
from bot.poller.steam_fetcher import SteamFetcher
from bot.services.models import ParsedAchievement
from bot.services.steam.client import OwnedGame, SteamApiError, SteamPresence

TG_ID = 42
STEAM_ID = "76561197960287930"


def parsed(achievement_id: str, appid: str = "550") -> ParsedAchievement:
    return ParsedAchievement(
        achievement_id=achievement_id,
        title_id=appid,
        title_name=None,
        name=f"Achievement {achievement_id}",
        description=None,
        icon_url=None,
        unlocked_at=None,
        gamerscore=0,
        rarity_percent=42.0,
        platform="steam",
    )


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[list[AchievementRow]] = []

    async def publish(self, tg_id, xuid, gamertag, achievements, title_name=None) -> None:
        self.published.append(list(achievements))


async def _linked_user(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await repo.link_platform_account(TG_ID, "steam", STEAM_ID, "Mad Omsk")


async def test_poll_title_publishes_only_new_achievements(
    repo: Repo, monkeypatch
) -> None:
    await _linked_user(repo)
    by_appid = {"550": [parsed("a1"), parsed("a2")]}

    async def fake_fetch_unlocked(repo_, api_key, steam_id, appid):
        return by_appid.get(appid, [])

    monkeypatch.setattr(steam_fetcher_module, "fetch_unlocked", fake_fetch_unlocked)
    publisher = FakePublisher()
    fetcher = SteamFetcher(repo, "key", publisher)  # type: ignore[arg-type]

    assert await fetcher.poll_title(TG_ID, STEAM_ID, "Mad Omsk", "550", "L4D2") == 2
    # Same answer a tick later: nothing new, nothing published.
    assert await fetcher.poll_title(TG_ID, STEAM_ID, "Mad Omsk", "550", "L4D2") == 0
    assert len(publisher.published) == 1

    by_appid["550"].append(parsed("a3"))
    assert await fetcher.poll_title(TG_ID, STEAM_ID, "Mad Omsk", "550", "L4D2") == 1
    assert [a.achievement_id for a in publisher.published[1]] == ["a3"]


async def test_refresh_user_polls_the_current_game(repo: Repo, monkeypatch) -> None:
    """The admin panel's own "🔄 Обновить Steam" (2026-09-05 follow-up) —
    never existed before, unlike Xbox's Fetcher.refresh_user()."""
    await _linked_user(repo)

    async def fake_batch(api_key, steam_ids):
        assert steam_ids == [STEAM_ID]
        return {
            STEAM_ID: SteamPresence(
                steam_id=STEAM_ID,
                persona_name="Mad Omsk",
                persona_state=1,
                gameid="550",
                game_name="Left 4 Dead 2",
            )
        }

    async def fake_fetch_unlocked(repo_, api_key, steam_id, appid):
        return [parsed("a1", appid)]

    monkeypatch.setattr(steam_fetcher_module, "get_presence_batch", fake_batch)
    monkeypatch.setattr(steam_fetcher_module, "fetch_unlocked", fake_fetch_unlocked)
    fetcher = SteamFetcher(repo, "key", FakePublisher())  # type: ignore[arg-type]

    summary = await fetcher.refresh_user(TG_ID, STEAM_ID, "Mad Omsk")

    assert "Left 4 Dead 2" in summary
    assert "1" in summary
    presence = await repo.steam_presence_of(STEAM_ID)
    assert presence is not None and presence.gameid == "550"


async def test_refresh_user_reports_offline_with_no_game(repo: Repo, monkeypatch) -> None:
    await _linked_user(repo)

    async def fake_batch(api_key, steam_ids):
        return {
            STEAM_ID: SteamPresence(
                steam_id=STEAM_ID, persona_name="Mad Omsk", persona_state=0, gameid=None,
                game_name=None,
            )
        }

    monkeypatch.setattr(steam_fetcher_module, "get_presence_batch", fake_batch)
    fetcher = SteamFetcher(repo, "key", FakePublisher())  # type: ignore[arg-type]

    summary = await fetcher.refresh_user(TG_ID, STEAM_ID, "Mad Omsk")

    assert "не в сети" in summary


async def test_refresh_user_handles_a_missing_profile(repo: Repo, monkeypatch) -> None:
    await _linked_user(repo)

    async def fake_batch(api_key, steam_ids):
        return {}  # Steam simply omits a deleted/hidden profile

    monkeypatch.setattr(steam_fetcher_module, "get_presence_batch", fake_batch)
    fetcher = SteamFetcher(repo, "key", FakePublisher())  # type: ignore[arg-type]

    summary = await fetcher.refresh_user(TG_ID, STEAM_ID, "Mad Omsk")

    assert "не" in summary.lower()


async def test_backfill_publishes_nothing(repo: Repo, monkeypatch) -> None:
    """The whole point of SPEC 5.6/9's backfill: the first link must be silent."""
    await _linked_user(repo)

    async def fake_get_owned_games(api_key, steam_id):
        return [OwnedGame(appid="550", name="L4D2", playtime_forever=100)]

    async def fake_fetch_unlocked(repo_, api_key, steam_id, appid):
        return [parsed("a1", appid), parsed("a2", appid)]

    monkeypatch.setattr(steam_fetcher_module, "get_owned_games", fake_get_owned_games)
    monkeypatch.setattr(steam_fetcher_module, "fetch_unlocked", fake_fetch_unlocked)
    publisher = FakePublisher()
    fetcher = SteamFetcher(repo, "key", publisher)  # type: ignore[arg-type]

    stored = await fetcher.backfill(TG_ID, STEAM_ID)

    assert stored == 2
    assert publisher.published == []


async def test_backfill_isolates_a_failing_game(repo: Repo, monkeypatch) -> None:
    """One game's SteamApiError must not sink the whole backfill (SPEC 9,
    M-Steam-2d) — same "one bad game doesn't ruin the rest" isolation
    Xbox's own backfill doesn't need (it has no per-game loop at all)."""
    await _linked_user(repo)

    async def fake_get_owned_games(api_key, steam_id):
        return [
            OwnedGame(appid="1", name="Broken Game", playtime_forever=10),
            OwnedGame(appid="2", name="Fine Game", playtime_forever=20),
        ]

    async def fake_fetch_unlocked(repo_, api_key, steam_id, appid):
        if appid == "1":
            raise SteamApiError("boom")
        return [parsed("a1", appid)]

    monkeypatch.setattr(steam_fetcher_module, "get_owned_games", fake_get_owned_games)
    monkeypatch.setattr(steam_fetcher_module, "fetch_unlocked", fake_fetch_unlocked)
    publisher = FakePublisher()
    fetcher = SteamFetcher(repo, "key", publisher)  # type: ignore[arg-type]

    stored = await fetcher.backfill(TG_ID, STEAM_ID)

    assert stored == 1  # only the fine game's achievement made it in
