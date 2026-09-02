"""Daily summary (SPEC 5.7, 7.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.db.repo import AchievementRow, Repo
from bot.poller.daily import DailySummary
from bot.services.stats import day_start_utc
from bot.util import utcnow

CHAT_ID = -100500
XUID_A = "xuid-a"
XUID_B = "xuid-b"


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent.append((chat_id, text))


def achievement(
    achievement_id: str, unlocked_at: datetime, score: int = 10, rarity: float | None = 50.0
) -> AchievementRow:
    return AchievementRow(
        title_id="1",
        achievement_id=achievement_id,
        name=achievement_id,
        description=None,
        icon_url=None,
        unlocked_at=unlocked_at.isoformat(timespec="seconds"),
        gamerscore=score,
        rarity_percent=rarity,
        platform="modern",
    )


async def _chat_with_two_players(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    for tg_id, xuid, tag in ((1, XUID_A, "Igor"), (2, XUID_B, "Alex")):
        await repo.ensure_user(tg_id, tag.lower())
        await repo.link_xbox_account(tg_id, xuid, tag, 1000)
        await repo.subscribe(CHAT_ID, tg_id)


async def test_summary_lists_everyone_and_marks_rare(repo: Repo) -> None:
    await _chat_with_two_players(repo)
    now = utcnow()
    await repo.insert_new_achievements(
        XUID_A,
        [
            achievement("a1", now, 50, rarity=2.4),
            achievement("a2", now, 40, rarity=30.0),
        ],
        is_backfill=False,
    )
    # Backfilled rows count too: the summary is a report, not the feed.
    await repo.insert_new_achievements(
        XUID_B, [achievement("b1", now, 20, rarity=None)], is_backfill=True
    )

    text = await DailySummary(FakeBot(), repo)._build(CHAT_ID, 0, 10.0, now.date())

    assert text is not None
    assert "Igor" in text and "Alex" in text
    assert "💎 1" in text  # only the 2.4% one is rare at a 10% threshold
    assert "Всего за день: 3 ачивки, +110 G" in text
    assert "За месяц:" in text


async def test_silent_when_nobody_unlocked_anything(repo: Repo) -> None:
    """Otherwise the summary becomes daily noise (SPEC 5.7)."""
    await _chat_with_two_players(repo)
    assert await DailySummary(FakeBot(), repo)._build(CHAT_ID, 0, 10.0, utcnow().date()) is None


async def test_yesterday_does_not_count_as_today(repo: Repo) -> None:
    await _chat_with_two_players(repo)
    yesterday = day_start_utc(0) - timedelta(hours=1)
    await repo.insert_new_achievements(XUID_A, [achievement("old", yesterday)], is_backfill=False)
    assert await DailySummary(FakeBot(), repo)._build(CHAT_ID, 0, 10.0, utcnow().date()) is None


async def test_summary_is_sent_once_per_day(repo: Repo) -> None:
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)
    await repo.set_app_setting("timezone", "UTC")
    await repo.set_app_setting("daily_summary_time", datetime.now(UTC).strftime("%H:%M"))

    bot = FakeBot()
    job = DailySummary(bot, repo)
    await job.tick()
    await job.tick()

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == CHAT_ID


async def test_disabled_chat_gets_nothing(repo: Repo) -> None:
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)
    await repo.update_chat_settings(CHAT_ID, daily_summary=0)
    await repo.set_app_setting("timezone", "UTC")
    await repo.set_app_setting("daily_summary_time", datetime.now(UTC).strftime("%H:%M"))

    bot = FakeBot()
    await DailySummary(bot, repo).tick()

    assert bot.sent == []
