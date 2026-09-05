"""Admin panel visibility into Steam accounts (2026-09-05 follow-up) —
found live: a Steam-only person was completely invisible to /admin
(admin_users() filtered `WHERE u.xuid IS NOT NULL`), and even someone with
both platforms only ever saw their Xbox achievement count in the list."""

from __future__ import annotations

from bot.db.repo import AchievementRow, AdminUserRow, Repo
from bot.handlers.admin import _home, _icon
from bot.util import utcnow

XUID = "xuid-a"
STEAM_ID = "76561197960287930"


async def test_admin_users_includes_a_steam_only_person(repo: Repo) -> None:
    await repo.ensure_user(1, "steamonly")
    await repo.link_platform_account(1, "steam", STEAM_ID, "SteamOnly")

    users = await repo.admin_users()

    assert len(users) == 1
    assert users[0].xuid is None
    assert users[0].steam_id == STEAM_ID
    assert users[0].steam_name == "SteamOnly"


async def test_admin_users_includes_someone_with_both_platforms(repo: Repo) -> None:
    await repo.ensure_user(1, "both")
    await repo.link_xbox_account(1, XUID, "Both", 0)
    await repo.link_platform_account(1, "steam", STEAM_ID, "BothSteam")

    users = await repo.admin_users()

    assert len(users) == 1
    assert users[0].xuid == XUID
    assert users[0].steam_id == STEAM_ID


async def test_achievement_counts_by_tg_id_sums_every_platform(repo: Repo) -> None:
    """The admin users list's own combined counter — used to be
    achievement_counts_by_xuid, which showed 0 for a Steam-only person and
    only the Xbox half for someone with both."""
    await repo.ensure_user(1, "both")
    await repo.link_xbox_account(1, XUID, "Both", 0)
    await repo.link_platform_account(1, "steam", STEAM_ID, "BothSteam")
    now = utcnow()
    await repo.insert_new_achievements(
        XUID,
        [
            AchievementRow(
                title_id="1",
                achievement_id="a1",
                name="A",
                description=None,
                icon_url=None,
                unlocked_at=now.isoformat(timespec="seconds"),
                gamerscore=10,
                rarity_percent=50.0,
                platform="modern",
            )
        ],
        is_backfill=False,
    )
    await repo.insert_new_achievements_steam(
        1,
        STEAM_ID,
        [
            AchievementRow(
                title_id="550",
                achievement_id="s1",
                name="S",
                description=None,
                icon_url=None,
                unlocked_at=now.isoformat(timespec="seconds"),
                gamerscore=0,
                rarity_percent=None,
                platform="steam",
            )
        ],
        is_backfill=False,
    )

    counts = await repo.achievement_counts_by_tg_id(None)

    assert counts[1] == (2, 10)


async def test_steam_presence_of_round_trips(repo: Repo) -> None:
    await repo.ensure_user(1, "someone")
    await repo.link_platform_account(1, "steam", STEAM_ID, "Someone")

    assert await repo.steam_presence_of(STEAM_ID) is None

    await repo.save_steam_presence_state(STEAM_ID, 1, "550", "Left 4 Dead 2", changed=True)
    row = await repo.steam_presence_of(STEAM_ID)

    assert row is not None
    assert row.persona_state == 1
    assert row.gameid == "550"
    assert row.game_name == "Left 4 Dead 2"


def _row(
    *, xuid: str | None, steam_id: str | None, token_status: str | None = None
) -> AdminUserRow:
    return AdminUserRow(
        tg_id=1,
        gamertag="Igor",
        username=None,
        xuid=xuid,
        gamerscore=0,
        is_excluded=False,
        last_online_at=None,
        token_status=token_status,
        last_refresh_at=None,
        steam_id=steam_id,
        steam_name="Igor" if steam_id else None,
    )


def test_icon_shows_platform_dots() -> None:
    assert _icon(_row(xuid=XUID, steam_id=None, token_status="active")) == "🟢✅"
    assert _icon(_row(xuid=None, steam_id=STEAM_ID)) == "⚫"
    assert _icon(_row(xuid=XUID, steam_id=STEAM_ID, token_status="invalid")) == "🟢⚠️⚫"


def test_icon_excluded_overrides_platform_dots() -> None:
    row = _row(xuid=XUID, steam_id=STEAM_ID)
    row.is_excluded = True
    assert _icon(row) == "🚫"


class _FakeUsageFetcher:
    def api_usage(self) -> list[tuple[int, int, float]]:
        return []


async def test_home_does_not_count_a_steam_only_person_as_a_broken_xbox_login(
    repo: Repo,
) -> None:
    """Found while adding Steam to the admin panel (2026-09-05): a
    Steam-only person has token_status=None (no Xbox token row at all),
    and `!= "active"` alone counted that as "без входа" — a broken Xbox
    login, not "no Xbox at all". The two are shown separately now."""
    await repo.ensure_user(1, "steamonly")
    await repo.link_platform_account(1, "steam", STEAM_ID, "SteamOnly")

    text, _markup = await _home(repo, _FakeUsageFetcher(), _FakeUsageFetcher())  # type: ignore[arg-type]

    assert "XBOX:  0" in text
    assert "Steam: 1" in text
