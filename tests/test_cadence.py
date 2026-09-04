"""poller/cadence.py — shared timing math, extracted out of presence.py and
steam_presence.py (they used to duplicate this byte-for-byte). Covered
indirectly through both pollers' own tests too, but pure functions this
small deserve direct coverage rather than only being exercised as a side
effect of a full tick().
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.poller.cadence import IDLE_AFTER_SECONDS, debounce_passed, is_due, presence_interval

NOW = datetime.now(UTC)


def _ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def test_is_due_when_never_updated() -> None:
    assert is_due(None, 120) is True


def test_is_due_respects_the_interval() -> None:
    assert is_due(_ago(200), 120) is True
    assert is_due(_ago(10), 120) is False


def test_debounce_passed_when_never_polled() -> None:
    assert debounce_passed(None, 120) is True


def test_debounce_passed_respects_the_interval() -> None:
    assert debounce_passed(_ago(200), 120) is True
    assert debounce_passed(_ago(10), 120) is False


def test_presence_interval_online_in_game() -> None:
    assert (
        presence_interval(
            online=True,
            in_game=True,
            changed_at=None,
            interval_in_game=60,
            interval_online=120,
            interval_offline=300,
            interval_idle=900,
        )
        == 60
    )


def test_presence_interval_online_not_in_game() -> None:
    assert (
        presence_interval(
            online=True,
            in_game=False,
            changed_at=None,
            interval_in_game=60,
            interval_online=120,
            interval_offline=300,
            interval_idle=900,
        )
        == 120
    )


def test_presence_interval_offline_recently() -> None:
    assert (
        presence_interval(
            online=False,
            in_game=False,
            changed_at=_ago(60),
            interval_in_game=60,
            interval_online=120,
            interval_offline=300,
            interval_idle=900,
        )
        == 300
    )


def test_presence_interval_idle_after_a_long_offline_stretch() -> None:
    assert (
        presence_interval(
            online=False,
            in_game=False,
            changed_at=_ago(IDLE_AFTER_SECONDS + 60),
            interval_in_game=60,
            interval_online=120,
            interval_offline=300,
            interval_idle=900,
        )
        == 900
    )


def test_presence_interval_offline_with_no_changed_at_is_not_idle() -> None:
    """No changed_at at all reads as "just now" (offline_for = 0), not as
    idle — same defensive default the pollers relied on before extraction."""
    assert (
        presence_interval(
            online=False,
            in_game=False,
            changed_at=None,
            interval_in_game=60,
            interval_online=120,
            interval_offline=300,
            interval_idle=900,
        )
        == 300
    )
