"""Admin panel helpers that don't need a router (SPEC 6.4)."""

from __future__ import annotations

from bot.handlers.admin import (
    LIMIT_MAX,
    LIMIT_MIN,
    RARE_THRESHOLD_MAX,
    RARE_THRESHOLD_MIN,
    _format_api_usage,
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
