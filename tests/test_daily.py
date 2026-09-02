"""Daily summary (SPEC 5.7, 7.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.db.repo import AchievementRow, Repo
from bot.poller.daily import DailySummary, build_summary
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

    text = await build_summary(repo, CHAT_ID, 10.0, now.date())

    assert text is not None
    assert "Igor" in text and "Alex" in text
    assert "24 часа" in text and "30 дней" in text  # not the same label twice
    assert "<pre>" in text and "</pre>" in text  # a monospace table, not prose
    assert text.count("Всего") == 2  # one total per window


async def test_zero_scorers_still_appear(repo: Repo) -> None:
    """A subscriber with nothing unlocked used to vanish from the table
    entirely — the roster should show him at zero, not hide him."""
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)

    text = await build_summary(repo, CHAT_ID, 10.0, utcnow().date())

    assert text is not None
    assert "Alex" in text  # unlocked nothing, still listed


async def test_gamertag_is_escaped_inside_the_html_table(repo: Repo) -> None:
    """The table is an HTML <pre> block; an unescaped "<" or "&" in a
    gamertag would break the markup Telegram parses."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    await repo.ensure_user(1, "weird")
    await repo.link_xbox_account(1, XUID_A, "A&B<C>", 1000)
    await repo.subscribe(CHAT_ID, 1)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)

    text = await build_summary(repo, CHAT_ID, 10.0, utcnow().date())

    assert text is not None
    assert "<C>" not in text  # would be parsed as a (bogus) HTML tag
    assert "&amp;" in text and "&lt;" in text


async def test_silent_when_nobody_unlocked_anything(repo: Repo) -> None:
    """Otherwise the summary becomes daily noise (SPEC 5.7)."""
    await _chat_with_two_players(repo)
    assert await build_summary(repo, CHAT_ID, 10.0, utcnow().date()) is None


async def test_window_is_a_rolling_day_not_a_calendar_one(repo: Repo) -> None:
    """The summary fires at 23:00, so a calendar window would leave 23:00–00:00
    in no report at all — every day would lose its last hour."""
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(
        XUID_A, [achievement("too-old", utcnow() - timedelta(hours=25))], is_backfill=False
    )
    assert await build_summary(repo, CHAT_ID, 10.0, utcnow().date()) is None

    # 23 hours ago is still inside the window, even though it is another
    # calendar day for someone.
    await repo.insert_new_achievements(
        XUID_A,
        [achievement("late-yesterday", utcnow() - timedelta(hours=23))],
        is_backfill=False,
    )
    text = await build_summary(repo, CHAT_ID, 10.0, utcnow().date())
    assert text is not None and "Igor" in text


async def test_month_window_is_thirty_rolling_days(repo: Repo) -> None:
    """ "30 дней" means exactly that, not the calendar month-to-date."""
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("today", utcnow())], is_backfill=False)
    await repo.insert_new_achievements(
        XUID_A,
        [achievement("old", utcnow() - timedelta(days=40))],
        is_backfill=False,
    )

    text = await build_summary(repo, CHAT_ID, 10.0, utcnow().date())

    assert text is not None
    # Two total achievements (today + old) must not both land in the 30-day
    # section; only the recent one should count there.
    assert "2 ачивки" not in text


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
