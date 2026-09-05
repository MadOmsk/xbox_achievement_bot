"""Admin panel helpers that don't need a router (SPEC 6.4)."""

from __future__ import annotations

from bot.handlers.admin import (
    LIMIT_MAX,
    LIMIT_MIN,
    NUMERIC_SETTINGS,
    ONLINE_REFRESH_INTERVAL_KEY,
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
    ttl_min/online_refresh_interval_min's own 0 means "off" instead
    (2026-09-05 follow-up), a different zero_label but the same
    allowed-at-zero treatment."""
    zero_allowed = {key for key, spec in NUMERIC_SETTINGS.items() if spec.min == 0}
    assert zero_allowed == {
        "summary_top_limit",
        "stats_games_limit",
        SYSTEM_MESSAGE_TTL_KEY,
        ONLINE_REFRESH_INTERVAL_KEY,
    }


def test_format_limit_shows_unlimited_for_zero() -> None:
    assert _format_limit("summary_top_limit", "0") == UNLIMITED_LABEL
    assert _format_limit("summary_top_limit", "15") == "15"


def test_every_numeric_setting_default_is_within_its_own_bounds() -> None:
    """Would have caught a typo'd bound the moment it landed, rather than
    only when an admin happened to hit it (2026-09-05, NUMERIC_SETTINGS
    registry refactor)."""
    for key, spec in NUMERIC_SETTINGS.items():
        assert spec.min <= spec.default <= spec.max, key


def test_format_limit_shows_off_for_zero_ttl() -> None:
    """system_message_ttl_min's own zero reads as "выключено", not
    "без ограничения" — 0 minutes isn't an unlimited TTL, it disables
    the auto-delete entirely (2026-09-05 follow-up)."""
    assert _format_limit(SYSTEM_MESSAGE_TTL_KEY, "0") == "выключено"
