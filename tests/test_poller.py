"""Deduplication, backfill and who the poller is allowed to touch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from bot.db.repo import AchievementRow, Repo
from bot.poller.fetcher import Fetcher
from bot.poller.reminders import MAX_REMINDERS, REMINDER_INTERVAL_HOURS, ReminderJob
from bot.services.xbox.models import ParsedAchievement
from bot.util import utcnow

TG_ID = 42
XUID = "2533274829605736"


def parsed(achievement_id: str, title_id: str = "1", platform: str = "modern") -> ParsedAchievement:
    return ParsedAchievement(
        achievement_id=achievement_id,
        title_id=title_id,
        title_name="Gears of War",
        name=f"Achievement {achievement_id}",
        description=None,
        icon_url=None,
        unlocked_at=None,
        gamerscore=10,
        rarity_percent=42.0,
        platform=platform,  # type: ignore[arg-type]
    )


@dataclass
class FakeHistoryEntry:
    title_id: str
    name: str
    platform: str
    current_gamerscore: int | None = 100
    max_gamerscore: int | None = 1000
    achievements_unlocked: int | None = 5
    achievements_total: int | None = 50
    last_played_at: str | None = "2026-09-01T10:00:00+00:00"
    icon_url: str | None = None


class FakeClient:
    def __init__(self, by_title=None, everything=None, history=None) -> None:
        self.by_title = by_title or {}
        self.everything = everything or []
        self.history = history or []
        self.title_calls: list[tuple[str, str]] = []
        self.resolved: list[str] = []
        self.resolvable: dict[str, FakeHistoryEntry] = {}

    async def title_achievements(self, tg_id, title_id, platform):
        self.title_calls.append((title_id, platform))
        return self.by_title.get(title_id, [])

    async def all_achievements(self, tg_id):
        return self.everything

    async def title_history(self, tg_id, max_items: int = 200):
        return self.history

    async def gamerscore(self, tg_id):
        return 35776

    async def resolve_title(self, tg_id, title_id):
        self.resolved.append(title_id)
        return self.resolvable.get(title_id)


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[list[AchievementRow]] = []

    async def publish(self, tg_id, xuid, gamertag, achievements, title_name=None) -> None:
        self.published.append(list(achievements))


async def _connected_user(repo: Repo, cipher) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await repo.save_refresh_token(TG_ID, cipher.encrypt("refresh"))
    await repo.link_xbox_account(TG_ID, XUID, "Mad Omsk", None)


async def test_dedup_publishes_each_achievement_once(repo: Repo, cipher) -> None:
    await _connected_user(repo, cipher)
    client = FakeClient(by_title={"1": [parsed("a1"), parsed("a2")]})
    publisher = FakePublisher()
    fetcher = Fetcher(repo, client, publisher)  # type: ignore[arg-type]

    assert await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "1", "modern", "Gears") == 2
    # Same answer from Xbox Live a minute later: nothing new, nothing published.
    assert await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "1", "modern", "Gears") == 0
    assert len(publisher.published) == 1

    client.by_title["1"].append(parsed("a3"))
    assert await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "1", "modern", "Gears") == 1
    assert [a.achievement_id for a in publisher.published[1]] == ["a3"]


async def test_backfill_publishes_nothing(repo: Repo, cipher) -> None:
    """The whole point of SPEC 5.6: the first connect must be silent."""
    await _connected_user(repo, cipher)
    client = FakeClient(
        everything=[parsed(str(i)) for i in range(50)],
        history=[FakeHistoryEntry("1", "Gears of War", "modern")],
    )
    publisher = FakePublisher()
    fetcher = Fetcher(repo, client, publisher)  # type: ignore[arg-type]

    stored = await fetcher.backfill(TG_ID, XUID)

    assert stored == 50
    assert publisher.published == []
    assert await repo.has_any_achievements(XUID)


async def test_backfill_covers_x360_titles_separately(repo: Repo, cipher) -> None:
    """Contract 2 does not list Xbox 360 achievements — verified live. Without
    the extra pass the first x360 session would look like fresh unlocks."""
    await _connected_user(repo, cipher)
    client = FakeClient(
        everything=[parsed("m1")],
        by_title={"360": [parsed("x1", title_id="360", platform="x360")]},
        history=[
            FakeHistoryEntry("1", "Modern Game", "modern"),
            FakeHistoryEntry("360", "Gears of War 3", "x360"),
        ],
    )
    fetcher = Fetcher(repo, client, FakePublisher())  # type: ignore[arg-type]

    await fetcher.backfill(TG_ID, XUID)

    assert client.title_calls == [("360", "x360")]
    # Now the same x360 achievement arrives from a real session: already seen.
    assert await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "360", "x360", "Gears 3") == 0


async def test_excluded_user_is_not_polled(repo: Repo, cipher) -> None:
    await _connected_user(repo, cipher)
    assert [t.tg_id for t in await repo.pollable_users()] == [TG_ID]

    await repo._conn.execute("UPDATE users SET is_excluded = 1 WHERE tg_id = ?", (TG_ID,))
    await repo._conn.commit()
    assert await repo.pollable_users() == []


async def test_dead_token_user_is_not_polled(repo: Repo, cipher) -> None:
    await _connected_user(repo, cipher)
    await repo.set_token_status(TG_ID, "invalid")
    assert await repo.pollable_users() == []


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(chat_id)


async def test_reminders_stop_after_three(repo: Repo, cipher) -> None:
    """A person may have left on purpose; a bot that nags forever gets blocked."""
    await _connected_user(repo, cipher)
    await repo.set_token_status(TG_ID, "invalid")
    bot = FakeBot()
    job = ReminderJob(bot, repo)  # type: ignore[arg-type]

    for _ in range(MAX_REMINDERS + 2):
        # Pretend the interval has passed, otherwise nothing would be due.
        await repo._conn.execute(
            "UPDATE tokens SET last_notified_at = '2000-01-01T00:00:00+00:00' "
            "WHERE tg_id = ? AND notify_count > 0",
            (TG_ID,),
        )
        await repo._conn.commit()
        await job.run()

    assert len(bot.sent) == MAX_REMINDERS


async def test_reminder_respects_the_interval(repo: Repo, cipher) -> None:
    await _connected_user(repo, cipher)
    await repo.set_token_status(TG_ID, "invalid")
    bot = FakeBot()
    job = ReminderJob(bot, repo)  # type: ignore[arg-type]

    await job.run()
    await job.run()  # immediately again — too early
    assert len(bot.sent) == 1
    assert await repo.tokens_needing_reminder(MAX_REMINDERS, REMINDER_INTERVAL_HOURS) == []


async def test_title_name_is_resolved_once_when_presence_has_none(repo: Repo, cipher) -> None:
    """Presence returns an empty name for PC titles; a published message must
    not say "неизвестная игра" because of it."""
    await _connected_user(repo, cipher)
    client = FakeClient(by_title={"85494077": [parsed("a1", title_id="85494077")]})
    client.resolvable["85494077"] = FakeHistoryEntry(
        "85494077", "Microsoft Solitaire Collection", "modern"
    )
    publisher = FakePublisher()
    fetcher = Fetcher(repo, client, publisher)  # type: ignore[arg-type]

    await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "85494077", "modern", None)
    assert client.resolved == ["85494077"]
    assert await repo.title_name("85494077") == "Microsoft Solitaire Collection"

    # Second time the name comes from the cache, not from Xbox Live.
    client.by_title["85494077"].append(parsed("a2", title_id="85494077"))
    await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "85494077", "modern", None)
    assert client.resolved == ["85494077"]


async def test_x360_achievements_get_the_games_box_art_as_their_icon(
    repo: Repo, cipher
) -> None:
    """Contract 1 never gives a per-achievement icon at all (verified live —
    a bare imageId int, no documented way to turn it into a URL) — the
    game's own box art (titlehub's display_image) stands in instead."""
    await _connected_user(repo, cipher)
    client = FakeClient(by_title={"360": [parsed("a1", title_id="360", platform="x360")]})
    client.resolvable["360"] = FakeHistoryEntry(
        "360", "Gears of War 3", "x360", icon_url="https://example/boxart.jpg"
    )
    publisher = FakePublisher()
    fetcher = Fetcher(repo, client, publisher)  # type: ignore[arg-type]

    await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "360", "x360", "Gears of War 3")

    assert publisher.published[0][0].icon_url == "https://example/boxart.jpg"
    assert await repo.title_icon_url("360") == "https://example/boxart.jpg"

    # Second title, same game: the icon comes from the cache, not another request.
    client.by_title["360"].append(parsed("a2", title_id="360", platform="x360"))
    await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "360", "x360", "Gears of War 3")
    assert client.resolved == ["360"]


async def test_upsert_title_does_not_blank_a_cached_icon(repo: Repo) -> None:
    """ensure_title_name() (fetcher.py) upserts just name/platform on every
    new title it resolves — it must not erase an icon_url a separate
    ensure_title_icon() call already cached for the same title."""
    await repo.upsert_title("360", "Gears of War 3", "x360", "https://example/boxart.jpg")
    await repo.upsert_title("360", "Gears of War 3", "x360")
    assert await repo.title_icon_url("360") == "https://example/boxart.jpg"


def parsed_at(achievement_id: str, when, title_id: str = "1") -> ParsedAchievement:
    item = parsed(achievement_id, title_id=title_id)
    item.unlocked_at = when
    return item


async def test_catch_up_publishes_only_what_is_fresh(repo: Repo, cipher) -> None:
    """After a fortnight of downtime a chat does not want the archive; after a
    one-minute restart nothing may be lost (SPEC 5.8)."""
    await _connected_user(repo, cipher)
    now = utcnow()
    client = FakeClient(
        by_title={
            "1": [
                parsed_at("recent", now - timedelta(hours=2)),
                parsed_at("ancient", now - timedelta(days=9)),
                parsed_at("undated", None),
            ]
        },
        history=[FakeHistoryEntry("1", "Gears of War", "modern")],
    )
    publisher = FakePublisher()
    fetcher = Fetcher(repo, client, publisher)  # type: ignore[arg-type]

    titles, published = await fetcher.catch_up(
        TG_ID, XUID, "Mad Omsk", now - timedelta(days=14), 24, 20
    )

    assert titles == 1
    assert published == 1
    assert [a.achievement_id for a in publisher.published[0]] == ["recent"]
    # The old ones are still recorded, so they never surface again as "new".
    assert await fetcher.poll_title(TG_ID, XUID, "Mad Omsk", "1", "modern", "Gears") == 0


async def test_catch_up_also_fills_x360_box_art(repo: Repo, cipher) -> None:
    """poll_title() and catch_up() both publish live x360 unlocks — the box
    art fallback (ensure_title_icon) must not be something only one of the
    two paths remembers to apply."""
    await _connected_user(repo, cipher)
    now = utcnow()
    item = parsed("a1", title_id="360", platform="x360")
    item.unlocked_at = now
    client = FakeClient(
        by_title={"360": [item]},
        history=[
            FakeHistoryEntry("360", "Gears of War 3", "x360", last_played_at=now.isoformat())
        ],
    )
    client.resolvable["360"] = FakeHistoryEntry(
        "360", "Gears of War 3", "x360", icon_url="https://example/boxart.jpg"
    )
    publisher = FakePublisher()
    fetcher = Fetcher(repo, client, publisher)  # type: ignore[arg-type]

    await fetcher.catch_up(TG_ID, XUID, "Mad Omsk", now - timedelta(days=1), 24, 20)

    assert publisher.published[0][0].icon_url == "https://example/boxart.jpg"


async def test_catch_up_skips_games_untouched_since_last_poll(repo: Repo, cipher) -> None:
    await _connected_user(repo, cipher)
    now = utcnow()
    client = FakeClient(
        by_title={"1": [parsed_at("a1", now)]},
        history=[
            FakeHistoryEntry(
                "1", "Gears of War", "modern", last_played_at=(now - timedelta(days=3)).isoformat()
            )
        ],
    )
    fetcher = Fetcher(repo, client, FakePublisher())  # type: ignore[arg-type]

    titles, published = await fetcher.catch_up(
        TG_ID, XUID, "Mad Omsk", now - timedelta(hours=1), 24, 20
    )

    assert (titles, published) == (0, 0)
    assert client.title_calls == []
