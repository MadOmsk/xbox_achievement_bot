"""poller/steam_presence.py: who gets polled, presence merging into
seen state, and the final-poll-of-the-old-game trigger (SPEC 9,
M-Steam-2c) — the Steam counterpart of presence.py, which itself has no
existing direct test file to mirror (only Fetcher/ReminderJob get one, in
test_poller.py) — coverage here is new, not a gap versus Xbox.
"""

from __future__ import annotations

from pydantic import SecretStr

from bot.config import Settings
from bot.db.repo import Repo
from bot.poller import steam_presence as steam_presence_module
from bot.poller.steam_presence import SteamPresencePoller
from bot.services.steam.client import SteamApiError, SteamPresence

TG_ID = 42
STEAM_ID = "76561197960287930"


class FakeFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str, str, str | None]] = []

    async def poll_title(self, tg_id, steam_id, persona_name, appid, game_name) -> int:
        self.calls.append((tg_id, steam_id, persona_name, appid, game_name))
        return 0


async def _linked_user(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await repo.link_platform_account(TG_ID, "steam", STEAM_ID, "Mad Omsk")


def _steam_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"steam_api_key": SecretStr("fake-key")})


async def test_tick_does_nothing_when_steam_is_not_configured(
    repo: Repo, settings: Settings, monkeypatch
) -> None:
    calls = {"n": 0}

    async def fake_pollable_users():
        calls["n"] += 1
        return []

    monkeypatch.setattr(repo, "steam_pollable_users", fake_pollable_users)
    # Explicit None, not just the fixture's default — Settings falls back to
    # reading the real .env otherwise, which has a real STEAM_API_KEY set.
    unconfigured = settings.model_copy(update={"steam_api_key": None})
    poller = SteamPresencePoller(unconfigured, repo, FakeFetcher())  # type: ignore[arg-type]

    await poller.tick()

    assert calls["n"] == 0  # never even asked who's pollable


async def test_tick_polls_a_freshly_started_game(
    repo: Repo, settings: Settings, monkeypatch
) -> None:
    await _linked_user(repo)
    steam_settings = _steam_settings(settings)

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

    monkeypatch.setattr(steam_presence_module, "get_presence_batch", fake_batch)
    fetcher = FakeFetcher()
    poller = SteamPresencePoller(steam_settings, repo, fetcher)  # type: ignore[arg-type]

    await poller.tick()

    assert fetcher.calls == [(TG_ID, STEAM_ID, "Mad Omsk", "550", "Left 4 Dead 2")]
    presence = await repo.steam_pollable_users()
    assert presence[0].gameid == "550"


async def test_tick_does_a_final_poll_of_the_old_game_when_it_changes(
    repo: Repo, settings: Settings, monkeypatch
) -> None:
    """An unlock is often right before quitting — same reasoning as Xbox's
    presence.py (SPEC 5.3)."""
    await _linked_user(repo)
    steam_settings = _steam_settings(settings)
    await repo.save_steam_presence_state(STEAM_ID, 1, "550", "Left 4 Dead 2", changed=True)
    # Backdated past the in-game interval, or _is_due() would skip this
    # target entirely — a real tick only ever sees a change at its next
    # due moment, never sooner (same as Xbox's presence.py).
    await repo._conn.execute(
        "UPDATE steam_presence_state SET updated_at = '2020-01-01T00:00:00+00:00' "
        "WHERE steam_id = ?",
        (STEAM_ID,),
    )
    await repo._conn.commit()

    async def fake_batch(api_key, steam_ids):
        # Quit the game, still online.
        return {
            STEAM_ID: SteamPresence(
                steam_id=STEAM_ID, persona_name="Mad Omsk", persona_state=1,
                gameid=None, game_name=None,
            )
        }

    monkeypatch.setattr(steam_presence_module, "get_presence_batch", fake_batch)
    fetcher = FakeFetcher()
    poller = SteamPresencePoller(steam_settings, repo, fetcher)  # type: ignore[arg-type]

    await poller.tick()

    # Polled the game that just ended, not a new one.
    assert fetcher.calls == [(TG_ID, STEAM_ID, "Mad Omsk", "550", "Left 4 Dead 2")]


async def test_grace_period_keeps_polling_a_game_that_briefly_vanished(
    repo: Repo, settings: Settings, monkeypatch
) -> None:
    """Steam's own presence sometimes stops reporting gameid for a few
    minutes while someone keeps playing — found live, cost an achievement a
    ~19 minute delay. As long as last_active_at is within the grace window
    and the person isn't reported fully offline, keep polling the last game
    seen (SPEC 9's follow-up)."""
    await _linked_user(repo)
    steam_settings = _steam_settings(settings)
    await repo.save_steam_presence_state(STEAM_ID, 1, "550", "Left 4 Dead 2", changed=True)
    # The vanish itself already happened on a prior tick (its own "final"
    # poll already fired then) — gameid is gone, but last_active_* still
    # points at the game, fresh.
    await repo.save_steam_presence_state(STEAM_ID, 1, None, None, changed=True)
    await repo._conn.execute(
        "UPDATE steam_presence_state SET updated_at = '2020-01-01T00:00:00+00:00' "
        "WHERE steam_id = ?",
        (STEAM_ID,),
    )
    await repo._conn.commit()

    async def fake_batch(api_key, steam_ids):
        # Still no gameid this tick either — same as last stored state, so
        # this is a steady-state tick, not a fresh change.
        return {
            STEAM_ID: SteamPresence(
                steam_id=STEAM_ID, persona_name="Mad Omsk", persona_state=1,
                gameid=None, game_name=None,
            )
        }

    monkeypatch.setattr(steam_presence_module, "get_presence_batch", fake_batch)
    fetcher = FakeFetcher()
    poller = SteamPresencePoller(steam_settings, repo, fetcher)  # type: ignore[arg-type]

    await poller.tick()

    assert fetcher.calls == [(TG_ID, STEAM_ID, "Mad Omsk", "550", "Left 4 Dead 2")]


async def test_grace_period_does_not_apply_once_reported_fully_offline(
    repo: Repo, settings: Settings, monkeypatch
) -> None:
    """persona_state == 0 means Steam itself says they're gone — no benefit
    of the doubt, unlike a merely-missing gameid."""
    await _linked_user(repo)
    steam_settings = _steam_settings(settings)
    await repo.save_steam_presence_state(STEAM_ID, 1, "550", "Left 4 Dead 2", changed=True)
    await repo.save_steam_presence_state(STEAM_ID, 0, None, None, changed=True)
    await repo._conn.execute(
        "UPDATE steam_presence_state SET updated_at = '2020-01-01T00:00:00+00:00' "
        "WHERE steam_id = ?",
        (STEAM_ID,),
    )
    await repo._conn.commit()

    async def fake_batch(api_key, steam_ids):
        return {
            STEAM_ID: SteamPresence(
                steam_id=STEAM_ID, persona_name="Mad Omsk", persona_state=0,
                gameid=None, game_name=None,
            )
        }

    monkeypatch.setattr(steam_presence_module, "get_presence_batch", fake_batch)
    fetcher = FakeFetcher()
    poller = SteamPresencePoller(steam_settings, repo, fetcher)  # type: ignore[arg-type]

    await poller.tick()

    assert fetcher.calls == []


async def test_grace_period_expires(repo: Repo, settings: Settings, monkeypatch) -> None:
    await _linked_user(repo)
    steam_settings = _steam_settings(settings)
    await repo.save_steam_presence_state(STEAM_ID, 1, "550", "Left 4 Dead 2", changed=True)
    await repo.save_steam_presence_state(STEAM_ID, 1, None, None, changed=True)
    await repo._conn.execute(
        "UPDATE steam_presence_state SET updated_at = '2020-01-01T00:00:00+00:00',"
        "  last_active_at = '2020-01-01T00:00:00+00:00' WHERE steam_id = ?",
        (STEAM_ID,),
    )
    await repo._conn.commit()

    async def fake_batch(api_key, steam_ids):
        return {
            STEAM_ID: SteamPresence(
                steam_id=STEAM_ID, persona_name="Mad Omsk", persona_state=1,
                gameid=None, game_name=None,
            )
        }

    monkeypatch.setattr(steam_presence_module, "get_presence_batch", fake_batch)
    fetcher = FakeFetcher()
    poller = SteamPresencePoller(steam_settings, repo, fetcher)  # type: ignore[arg-type]

    await poller.tick()

    assert fetcher.calls == []


async def test_tick_skips_a_profile_steam_did_not_return(
    repo: Repo, settings: Settings, monkeypatch
) -> None:
    await _linked_user(repo)
    steam_settings = _steam_settings(settings)

    async def fake_batch(api_key, steam_ids):
        return {}

    monkeypatch.setattr(steam_presence_module, "get_presence_batch", fake_batch)
    fetcher = FakeFetcher()
    poller = SteamPresencePoller(steam_settings, repo, fetcher)  # type: ignore[arg-type]

    await poller.tick()  # must not raise

    assert fetcher.calls == []


async def test_tick_survives_a_batch_failure(repo: Repo, settings: Settings, monkeypatch) -> None:
    await _linked_user(repo)
    steam_settings = _steam_settings(settings)

    async def fake_batch(api_key, steam_ids):
        raise SteamApiError("Steam is down")

    monkeypatch.setattr(steam_presence_module, "get_presence_batch", fake_batch)
    fetcher = FakeFetcher()
    poller = SteamPresencePoller(steam_settings, repo, fetcher)  # type: ignore[arg-type]

    await poller.tick()  # must not raise

    assert fetcher.calls == []
