"""Small shared helpers."""

from __future__ import annotations

from bot.util import cooldown_minutes_left, mask, parse_utc_offset, thousands


def test_cooldown_never_called_is_allowed_now() -> None:
    assert cooldown_minutes_left(None, 1000.0, 600) == 0


def test_cooldown_blocks_until_it_expires() -> None:
    # Called at t=0, checked at t=1 with a 600s cooldown: ~599s left, rounds up.
    assert cooldown_minutes_left(0.0, 1.0, 600) == 10


def test_cooldown_reaches_zero_exactly_at_expiry() -> None:
    assert cooldown_minutes_left(0.0, 600.0, 600) == 0
    assert cooldown_minutes_left(0.0, 599.0, 600) == 1


def test_thousands_separator() -> None:
    # U+2009, a thin space Telegram will not wrap the number on.
    assert thousands(152498) == f"152{chr(0x2009)}498"
    assert thousands(42) == "42"


def test_mask_reveals_only_length() -> None:
    assert mask(None) == "<empty>"
    assert mask("") == "<empty>"
    secret = mask("super-secret-token")
    assert "super-secret-token" not in secret
    assert str(len("super-secret-token")) in secret


def test_parse_utc_offset_basic_forms() -> None:
    assert parse_utc_offset("+3") == 180
    assert parse_utc_offset("-5") == -300
    assert parse_utc_offset("3") == 180  # missing sign means positive
    assert parse_utc_offset("+5:30") == 330
    assert parse_utc_offset("-9:45") == -585
    assert parse_utc_offset("UTC+3") == 180
    assert parse_utc_offset("utc-8") == -480


def test_parse_utc_offset_rejects_out_of_range_and_garbage() -> None:
    assert parse_utc_offset("+15") is None  # past UTC+14
    assert parse_utc_offset("-13") is None  # past UTC-12
    assert parse_utc_offset("not a timezone") is None
    assert parse_utc_offset("") is None
    assert parse_utc_offset("+3:99") is None  # not a real minute count
