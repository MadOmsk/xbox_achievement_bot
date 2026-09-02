"""Deduplication, backfill and who the poller is allowed to touch."""

from __future__ import annotations

from dataclasses import dataclass

from bot.db.repo import AchievementRow, Repo
from bot.poller.fetcher import Fetcher
from bot.poller.reminders import MAX_REMINDERS, REMINDER_INTERVAL_HOURS, ReminderJob
from bot.services.xbox.models import ParsedAchievement

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


class FakeClient:
    def __init__(self, by_title=None, everything=None, history=None) -> None:
        self.by_title = by_title or {}
        self.everything = everything or []
        self.history = history or []
        self.title_calls: list[tuple[str, str]] = []

    async def title_achievements(self, tg_id, title_id, platform):
        self.title_calls.append((title_id, platform))
        return self.by_title.get(title_id, [])

    async def all_achievements(self, tg_id):
        return self.everything

    async def title_history(self, tg_id, max_items: int = 200):
        return self.history

    async def gamerscore(self, tg_id):
        return 35776


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
