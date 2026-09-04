"""Daily summary (SPEC 5.7, 7.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import date as date_type

from bot.db.repo import AchievementRow, Repo
from bot.poller.daily import DailySummary, build_summary, full_leaderboard
from bot.util import utcnow

CHAT_ID = -100500
XUID_A = "xuid-a"
XUID_B = "xuid-b"


async def summary_text(repo: Repo, chat_id: int, threshold: float, today: date_type) -> str | None:
    """build_summary now also returns an optional "показать всех" keyboard —
    most of these tests only care about the text."""
    built = await build_summary(repo, chat_id, threshold, today)
    return built[0] if built is not None else None


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

    text = await summary_text(repo, CHAT_ID, 10.0, now.date())

    assert text is not None
    assert "Igor" in text and "Alex" in text
    # One totals line per window, label fused right into it (not a separate
    # "Всего" line any more — the label itself says which window it is).
    assert text.count("<b>24 часа:</b>") == 1
    assert text.count("<b>30 дней:</b>") == 1
    assert "<blockquote expandable>" in text and "</blockquote>" in text


async def test_zero_scorers_still_appear(repo: Repo) -> None:
    """A subscriber with nothing unlocked used to vanish from the table
    entirely — the roster should show him at zero, not hide him."""
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)

    text = await summary_text(repo, CHAT_ID, 10.0, utcnow().date())

    assert text is not None
    assert "Alex" in text  # unlocked nothing, still listed


async def test_gamertag_is_escaped_inside_the_html_table(repo: Repo) -> None:
    """The list lives inside a <blockquote>; an unescaped "<" or "&" in a
    gamertag would break the markup Telegram parses."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    await repo.ensure_user(1, "weird")
    await repo.link_xbox_account(1, XUID_A, "A&B<C>", 1000)
    await repo.subscribe(CHAT_ID, 1)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)

    text = await summary_text(repo, CHAT_ID, 10.0, utcnow().date())

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
    text = await summary_text(repo, CHAT_ID, 10.0, utcnow().date())
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

    text = await summary_text(repo, CHAT_ID, 10.0, utcnow().date())

    assert text is not None
    # Two total achievements (today + old) must not both land in the 30-day
    # section; only the recent one should count there.
    assert "2 ачивки" not in text


async def test_summary_is_sent_once_per_day(repo: Repo) -> None:
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)
    await repo.update_chat_settings(
        CHAT_ID, tz_offset_min=0, daily_summary_time=datetime.now(UTC).strftime("%H:%M")
    )

    bot = FakeBot()
    job = DailySummary(bot, repo)
    await job.tick()
    await job.tick()

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == CHAT_ID


async def test_summary_offers_show_all_button_only_past_the_configured_limit(
    repo: Repo,
) -> None:
    """The admin-configurable summary_top_limit (default 15) caps the table;
    past it a "Показать всех" button should appear, pointing at a fresh,
    uncapped re-fetch — not the original list carried over (SPEC 6.3)."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    for i in range(3):
        tg_id, xuid, tag = i + 1, f"xuid-{i}", f"Player{i}"
        await repo.ensure_user(tg_id, tag.lower())
        await repo.link_xbox_account(tg_id, xuid, tag, 0)
        await repo.subscribe(CHAT_ID, tg_id)
        await repo.insert_new_achievements(
            xuid, [achievement(f"a{i}", utcnow())], is_backfill=False
        )
    await repo.set_app_setting("summary_top_limit", "2")

    built = await build_summary(repo, CHAT_ID, 10.0, utcnow().date())

    assert built is not None
    text, markup = built
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.callback_data == "summary:all:day" for b in buttons)
    assert any(b.callback_data == "summary:all:month" for b in buttons)
    # Only 2 of the 3 players make it into the capped 24h table.
    day_section = text.split("30 дней")[0]
    assert sum(day_section.count(f"Player{i}") for i in range(3)) == 2


async def test_summary_top_limit_zero_means_no_cap(repo: Repo) -> None:
    """0 means "no cap" (SPEC 6.4) — everyone fits in the list, so there is
    nothing left for a «Показать всех» button to add."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)
    for i in range(3):
        tg_id, xuid, tag = i + 1, f"xuid-{i}", f"Player{i}"
        await repo.ensure_user(tg_id, tag.lower())
        await repo.link_xbox_account(tg_id, xuid, tag, 0)
        await repo.subscribe(CHAT_ID, tg_id)
        await repo.insert_new_achievements(
            xuid, [achievement(f"a{i}", utcnow())], is_backfill=False
        )
    await repo.set_app_setting("summary_top_limit", "0")

    built = await build_summary(repo, CHAT_ID, 10.0, utcnow().date())

    assert built is not None
    text, markup = built
    assert markup is None  # nothing truncated, nothing to show more of
    assert all(f"Player{i}" in text for i in range(3))

    full = await full_leaderboard(repo, CHAT_ID, 10.0, "day")
    assert full is not None
    assert all(f"Player{i}" in full for i in range(3))


async def test_summary_has_no_show_all_button_under_the_limit(repo: Repo) -> None:
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)

    built = await build_summary(repo, CHAT_ID, 10.0, utcnow().date())

    assert built is not None
    _text, markup = built
    assert markup is None


async def test_chat_rare_threshold_defaults_and_updates(repo: Repo) -> None:
    """Every chat has a real threshold from creation, no shared fallback
    (SPEC 5.5) — admin_chats()/publication_targets() both read this column."""
    await repo.upsert_chat(CHAT_ID, "Гейминг-чат", 1)

    chat = next(c for c in await repo.admin_chats() if c.chat_id == CHAT_ID)
    assert chat.rare_threshold_percent == 10.0  # the hardcoded default for new chats

    await repo.update_chat_settings(CHAT_ID, rare_threshold_percent=7.5)
    chat = next(c for c in await repo.admin_chats() if c.chat_id == CHAT_ID)
    assert chat.rare_threshold_percent == 7.5


async def test_chat_own_summary_time_and_zone_decide_when_it_fires(repo: Repo) -> None:
    """A chat's own daily_summary_time/tz_offset_min (SPEC 5.7) are what the
    scheduler checks — no shared value behind them any more."""
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)
    own_time = datetime.now(UTC).strftime("%H:%M")
    await repo.update_chat_settings(CHAT_ID, daily_summary_time=own_time, tz_offset_min=0)

    bot = FakeBot()
    await DailySummary(bot, repo).tick()

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == CHAT_ID


async def test_chat_own_time_does_not_fire_outside_its_own_slot(repo: Repo) -> None:
    """A time that does NOT match "now" must not fire."""
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)
    now = datetime.now(UTC).strftime("%H:%M")
    off_hour = "00:00" if now != "00:00" else "00:01"  # keep it distinct from "now"
    await repo.update_chat_settings(CHAT_ID, daily_summary_time=off_hour, tz_offset_min=0)

    bot = FakeBot()
    await DailySummary(bot, repo).tick()

    assert bot.sent == []


async def test_disabled_chat_gets_nothing(repo: Repo) -> None:
    await _chat_with_two_players(repo)
    await repo.insert_new_achievements(XUID_A, [achievement("a1", utcnow())], is_backfill=False)
    await repo.update_chat_settings(
        CHAT_ID,
        daily_summary=0,
        tz_offset_min=0,
        daily_summary_time=datetime.now(UTC).strftime("%H:%M"),
    )

    bot = FakeBot()
    await DailySummary(bot, repo).tick()

    assert bot.sent == []
