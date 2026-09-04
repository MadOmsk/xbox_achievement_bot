"""HowLongToBeat: search/cache service, chat-recent shortcuts, and the
handler's pure formatting/pagination logic (SPEC 6.6). No network — the
howlongtobeatpy calls themselves are out of scope for a unit test."""

from __future__ import annotations

from bot.db.repo import HltbCacheRow, Repo, TitleHistoryRow
from bot.handlers.hltb import _card, _int_setting, _label, _recent_keyboard, _results_keyboard
from bot.services.hltb import HltbResult, _clean, _clean_query, _from_cache_row

CHAT_ID = -100777


def result(hltb_id: int = 1, year: int | None = 2021) -> HltbResult:
    return HltbResult(
        hltb_id=hltb_id,
        name="Halo Infinite",
        release_year=year,
        main_hours=11.3,
        extra_hours=19.5,
        completionist_hours=29.2,
        platforms=["PC", "Xbox Series X/S"],
        game_url="https://howlongtobeat.com/game/1",
        image_url="https://howlongtobeat.com/games/1_Halo_Infinite.jpg",
    )


def test_clean_treats_zero_and_none_as_no_data() -> None:
    assert _clean(None) is None
    assert _clean(0) is None
    assert _clean(0.0) is None
    assert _clean(11.3) == 11.3


def test_clean_query_strips_trademark_symbols() -> None:
    # Real Xbox title names, not made up (SPEC 6.6) — HLTB's own search
    # chokes on this clutter that Xbox's titlehub happily includes.
    assert _clean_query("HELLDIVERS™ 2") == "HELLDIVERS 2"
    assert _clean_query("Minecraft Legends© - Windows") == "Minecraft Legends - Windows"
    assert _clean_query("Some Game®") == "Some Game"


def test_clean_query_strips_separator_punctuation_without_gluing_words() -> None:
    assert _clean_query("Halo: Reach") == "Halo Reach"
    assert _clean_query("Assassin's Creed, Valhalla") == "Assassin's Creed Valhalla"


def test_clean_query_collapses_the_extra_whitespace_it_creates() -> None:
    assert _clean_query("Game™:  Subtitle") == "Game Subtitle"


def test_clean_query_leaves_an_ordinary_title_untouched() -> None:
    assert _clean_query("Gears of War 3") == "Gears of War 3"


def test_from_cache_row_round_trips() -> None:
    row = HltbCacheRow(
        hltb_id=42,
        name="A Game",
        release_year=2020,
        main_hours=5.0,
        extra_hours=None,
        completionist_hours=15.0,
        platforms=["PS5"],
        game_url="https://howlongtobeat.com/game/42",
        image_url="https://howlongtobeat.com/games/42_A_Game.jpg",
    )
    r = _from_cache_row(row)
    assert (r.hltb_id, r.name, r.release_year) == (42, "A Game", 2020)
    assert (r.main_hours, r.extra_hours, r.completionist_hours) == (5.0, None, 15.0)
    assert r.platforms == ["PS5"]
    assert r.game_url == "https://howlongtobeat.com/game/42"
    assert r.image_url == "https://howlongtobeat.com/games/42_A_Game.jpg"


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
        platforms=[],
        game_url=None,
        image_url=None,
    )
    text = _card(incomplete)
    assert "Coop Only" in text
    assert "—" in text
    assert "None" not in text
    assert "Платформы" not in text  # nothing to show — no empty line either
    assert "howlongtobeat.com" not in text  # no link without a URL either


def test_card_lists_platforms_when_known() -> None:
    text = _card(result())
    assert "Платформы: PC, Xbox Series X/S" in text


def test_card_links_to_the_hltb_page_when_known() -> None:
    text = _card(result())
    assert '<a href="https://howlongtobeat.com/game/1">' in text


def test_card_uses_a_dot_separator_not_padding_spaces() -> None:
    text = _card(result())
    assert "Основной сюжет · 11.3 ч" in text
    assert "     " not in text  # the old manual-alignment padding is gone


def test_card_escapes_html_in_external_hltb_text() -> None:
    tricky = HltbResult(
        hltb_id=1,
        name="<b>Evil</b> & Co",
        release_year=None,
        main_hours=None,
        extra_hours=None,
        completionist_hours=None,
        platforms=["A & B"],
        game_url=None,
        image_url=None,
    )
    text = _card(tricky)
    assert "<b>Evil</b> & Co" not in text
    assert "&lt;b&gt;Evil&lt;/b&gt; &amp; Co" in text
    assert "A &amp; B" in text


def test_results_keyboard_paginates_five_per_page_with_nav() -> None:
    results = [result(hltb_id=i) for i in range(1, 13)]  # 12 -> 3 pages

    page0 = _results_keyboard(results, 0, 5)
    assert len(page0.inline_keyboard) == 7  # 5 picks + one nav row + cancel
    assert page0.inline_keyboard[-1][0].callback_data == "hltb:cancel"
    nav0 = page0.inline_keyboard[-2]
    assert [b.callback_data for b in nav0] == ["hltb:noop", "hltb:page:1"]  # no "back" on page 0
    assert nav0[0].text == "1/3"  # the page counter's label, not its (inert) callback_data

    page1 = _results_keyboard(results, 1, 5)
    nav1 = page1.inline_keyboard[-2]
    assert [b.callback_data for b in nav1] == ["hltb:page:0", "hltb:noop", "hltb:page:2"]
    assert nav1[1].text == "2/3"

    page2 = _results_keyboard(results, 2, 5)
    assert len(page2.inline_keyboard) == 4  # 2 leftover picks + nav + cancel
    nav2 = page2.inline_keyboard[-2]
    # no "forward" button on the last page
    assert [b.callback_data for b in nav2] == ["hltb:page:1", "hltb:noop"]


def test_results_keyboard_has_no_nav_row_for_a_single_page() -> None:
    results = [result(hltb_id=i) for i in range(1, 4)]
    markup = _results_keyboard(results, 0, 5)
    assert len(markup.inline_keyboard) == 4  # 3 picks + cancel, no nav
    picks, cancel = markup.inline_keyboard[:3], markup.inline_keyboard[3]
    assert all(row[0].callback_data.startswith("hltb:pick:") for row in picks)
    assert cancel[0].callback_data == "hltb:cancel"


def test_every_keyboard_offers_a_cancel_button() -> None:
    assert _results_keyboard([result()], 0, 5).inline_keyboard[-1][0].callback_data == "hltb:cancel"
    assert _recent_keyboard(["A"], 0, 5).inline_keyboard[-1][0].callback_data == "hltb:cancel"
    assert _recent_keyboard([], 0, 5).inline_keyboard[-1][0].callback_data == "hltb:cancel"


def test_results_keyboard_respects_a_custom_page_size() -> None:
    """The admin-configurable hltb_page_size (SPEC 6.4, 6.6) changes how many
    results/hints show per page, for both keyboards."""
    results = [result(hltb_id=i) for i in range(1, 5)]  # 4 results, page_size=2 -> 2 pages
    page0 = _results_keyboard(results, 0, 2)
    assert len(page0.inline_keyboard) == 4  # 2 picks + nav + cancel
    nav0 = page0.inline_keyboard[-2]
    assert nav0[0].text == "1/2"


def test_recent_keyboard_paginates_with_absolute_indices() -> None:
    """Button indices must stay absolute across pages — hltb_recent_pick
    looks games up by index into the *full* list, not the current page."""
    names = [f"Game {i}" for i in range(12)]  # 3 pages of 5

    page0 = _recent_keyboard(names, 0, 5)
    assert [row[0].callback_data for row in page0.inline_keyboard[:5]] == [
        f"hltb:qr:{i}" for i in range(5)
    ]
    nav0 = page0.inline_keyboard[-2]
    assert [b.callback_data for b in nav0] == ["hltb:noop", "hltb:rpage:1"]

    page2 = _recent_keyboard(names, 2, 5)
    assert [row[0].callback_data for row in page2.inline_keyboard[:2]] == [
        "hltb:qr:10",
        "hltb:qr:11",
    ]
    nav2 = page2.inline_keyboard[-2]
    assert [b.callback_data for b in nav2] == ["hltb:rpage:1", "hltb:noop"]


def test_recent_keyboard_has_no_nav_row_for_a_single_page() -> None:
    markup = _recent_keyboard(["A", "B"], 0, 5)
    assert len(markup.inline_keyboard) == 3  # 2 games + cancel, no nav


async def test_hltb_limits_are_admin_configurable(repo: Repo) -> None:
    assert await _int_setting(repo, "hltb_results_limit", "20") == 20  # default
    await repo.set_app_setting("hltb_results_limit", "7")
    assert await _int_setting(repo, "hltb_results_limit", "20") == 7

    assert await _int_setting(repo, "hltb_page_size", "5") == 5  # default
    await repo.set_app_setting("hltb_page_size", "3")
    assert await _int_setting(repo, "hltb_page_size", "5") == 3


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
