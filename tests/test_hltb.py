"""HowLongToBeat: search/cache service, chat-recent shortcuts, and the
handler's pure formatting/pagination logic (SPEC 6.6). No network — the
howlongtobeatpy calls themselves are out of scope for a unit test."""

from __future__ import annotations

from bot.db.repo import HltbCacheRow, Repo, TitleHistoryRow
from bot.handlers.hltb import _card, _label, _results_keyboard
from bot.services.hltb import HltbResult, _clean, _from_cache_row

CHAT_ID = -100777


def result(hltb_id: int = 1, year: int | None = 2021) -> HltbResult:
    return HltbResult(
        hltb_id=hltb_id,
        name="Halo Infinite",
        release_year=year,
        main_hours=11.3,
        extra_hours=19.5,
        completionist_hours=29.2,
    )


def test_clean_treats_zero_and_none_as_no_data() -> None:
    assert _clean(None) is None
    assert _clean(0) is None
    assert _clean(0.0) is None
    assert _clean(11.3) == 11.3


def test_from_cache_row_round_trips() -> None:
    row = HltbCacheRow(
        hltb_id=42,
        name="A Game",
        release_year=2020,
        main_hours=5.0,
        extra_hours=None,
        completionist_hours=15.0,
    )
    r = _from_cache_row(row)
    assert (r.hltb_id, r.name, r.release_year) == (42, "A Game", 2020)
    assert (r.main_hours, r.extra_hours, r.completionist_hours) == (5.0, None, 15.0)


def test_label_includes_year_when_known() -> None:
    assert _label(result(year=2021)) == "Halo Infinite (2021)"
    assert _label(result(year=None)) == "Halo Infinite"


def test_card_shows_a_dash_for_missing_completion_times() -> None:
    incomplete = HltbResult(
        hltb_id=1,
        name="Coop Only",
        release_year=None,
        main_hours=None,
        extra_hours=None,
        completionist_hours=None,
    )
    text = _card(incomplete)
    assert "Coop Only" in text
    assert "—" in text
    assert "None" not in text


def test_results_keyboard_paginates_five_per_page_with_nav() -> None:
    results = [result(hltb_id=i) for i in range(1, 13)]  # 12 -> 3 pages

    page0 = _results_keyboard(results, 0)
    assert len(page0.inline_keyboard) == 6  # 5 picks + one nav row
    nav0 = page0.inline_keyboard[-1]
    assert [b.callback_data for b in nav0] == ["hltb:noop", "hltb:page:1"]  # no "back" on page 0
    assert nav0[0].text == "1/3"  # the page counter's label, not its (inert) callback_data

    page1 = _results_keyboard(results, 1)
    nav1 = page1.inline_keyboard[-1]
    assert [b.callback_data for b in nav1] == ["hltb:page:0", "hltb:noop", "hltb:page:2"]
    assert nav1[1].text == "2/3"

    page2 = _results_keyboard(results, 2)
    assert len(page2.inline_keyboard) == 3  # only 2 leftover picks + nav
    nav2 = page2.inline_keyboard[-1]
    # no "forward" button on the last page
    assert [b.callback_data for b in nav2] == ["hltb:page:1", "hltb:noop"]


def test_results_keyboard_has_no_nav_row_for_a_single_page() -> None:
    results = [result(hltb_id=i) for i in range(1, 4)]
    markup = _results_keyboard(results, 0)
    assert len(markup.inline_keyboard) == 3
    assert all(row[0].callback_data.startswith("hltb:pick:") for row in markup.inline_keyboard)


async def test_hltb_cache_round_trip(repo: Repo) -> None:
    assert await repo.hltb_get_cached(99) is None

    await repo.hltb_cache_result(
        HltbCacheRow(
            hltb_id=99,
            name="Cached Game",
            release_year=2019,
            main_hours=8.0,
            extra_hours=12.0,
            completionist_hours=20.0,
        )
    )
    cached = await repo.hltb_get_cached(99)

    assert cached is not None
    assert cached.name == "Cached Game"
    assert cached.completionist_hours == 20.0


async def test_chat_recent_games_orders_by_recency_and_dedupes(repo: Repo) -> None:
    """Same membership as /online and /who: subscribers union chat_seen, not
    only publishers (SPEC 6.6)."""
    await repo.upsert_chat(CHAT_ID, "Игровой чат", 1)

    await repo.ensure_user(1, "publisher")
    await repo.link_xbox_account(1, "xuid-a", "Publisher", 0)
    await repo.subscribe(CHAT_ID, 1)

    await repo.ensure_user(2, "lurker")
    await repo.link_xbox_account(2, "xuid-b", "Lurker", 0)
    await repo.record_chat_seen(CHAT_ID, 2)  # in the chat, never subscribed

    await repo.ensure_user(3, "stranger")
    await repo.link_xbox_account(3, "xuid-c", "Stranger", 0)
    # tg_id 3 is connected but never seen or subscribed in this chat — must
    # not contribute games to it.

    def history(xuid: str, title_id: str, name: str, played_at: str) -> TitleHistoryRow:
        return TitleHistoryRow(
            title_id=title_id,
            name=name,
            platform="modern",
            current_gamerscore=0,
            max_gamerscore=0,
            achievements_unlocked=0,
            achievements_total=0,
            last_played_at=played_at,
        )

    await repo.save_title_history(
        "xuid-a",
        [history("xuid-a", "1", "Older Game", "2026-08-01T00:00:00+00:00")],
    )
    await repo.save_title_history(
        "xuid-b",
        [history("xuid-b", "2", "Newer Game", "2026-09-01T00:00:00+00:00")],
    )
    await repo.save_title_history(
        "xuid-c",
        [history("xuid-c", "3", "Stranger's Game", "2026-09-02T00:00:00+00:00")],
    )

    names = await repo.chat_recent_games(CHAT_ID)

    assert names == ["Newer Game", "Older Game"]
