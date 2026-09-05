"""Admin panel helpers that don't need a router (SPEC 6.4)."""

from __future__ import annotations

from bot.handlers.admin import (
    _LIMIT_MIN_OVERRIDES,
    LIMIT_MAX,
    LIMIT_MIN,
    RARE_THRESHOLD_MAX,
    RARE_THRESHOLD_MIN,
    SYSTEM_MESSAGE_TTL_KEY,
    UNLIMITED_LABEL,
    _format_api_usage,
    _format_limit,
)


def test_api_usage_formats_seconds_and_minutes() -> None:
    text = _format_api_usage([(3, 100, 15.0), (12, 300, 300.0)])
    assert text == "3/100 за 15с · 12/300 за 5 мин"


def test_api_usage_with_no_windows() -> None:
    assert _format_api_usage([]) == "нет данных"


def test_threshold_bounds_reject_zero_and_over_a_hundred() -> None:
    # 0 would mean "nothing is ever rare" — indistinguishable from a typo.
    assert not (RARE_THRESHOLD_MIN <= 0 <= RARE_THRESHOLD_MAX)
    assert not (RARE_THRESHOLD_MIN <= 100.5 <= RARE_THRESHOLD_MAX)
    assert RARE_THRESHOLD_MIN <= 7.5 <= RARE_THRESHOLD_MAX


def test_row_limit_bounds_reject_zero_and_absurdly_large() -> None:
    assert not (LIMIT_MIN <= 0 <= LIMIT_MAX)
    assert not (LIMIT_MIN <= 500 <= LIMIT_MAX)
    assert LIMIT_MIN <= 15 <= LIMIT_MAX


def test_only_summary_stats_and_ttl_limits_allow_zero() -> None:
    """0 means "no cap" (SPEC 6.4) — meaningful for a list inside a
    collapsible quote, not for hltb_page_size (feeds a keyboard grid) or
    hltb_results_limit (a search pool of 0 is just broken). system_message_
    ttl_min's own 0 means "off" instead (2026-09-05 follow-up), a different
    label (_ZERO_LABELS) but the same allowed-at-zero treatment."""
    assert _LIMIT_MIN_OVERRIDES == {
        "summary_top_limit": 0,
        "stats_games_limit": 0,
        SYSTEM_MESSAGE_TTL_KEY: 0,
    }


def test_format_limit_shows_unlimited_for_zero() -> None:
    assert _format_limit("summary_top_limit", "0") == UNLIMITED_LABEL
    assert _format_limit("summary_top_limit", "15") == "15"


def test_format_limit_shows_off_for_zero_ttl() -> None:
    """system_message_ttl_min's own zero reads as "выключено", not
    "без ограничения" — 0 minutes isn't an unlimited TTL, it disables
    the auto-delete entirely (2026-09-05 follow-up)."""
    assert _format_limit(SYSTEM_MESSAGE_TTL_KEY, "0") == "выключено"
