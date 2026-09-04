"""Shared timing math for the presence pollers (Xbox: `presence.py`, Steam:
`steam_presence.py`).

The two pollers watch different providers with different batching (one
account per request vs. a shared batch call) and different state shapes
(`state: str` vs `persona_state: int`), so `tick()`/`_handle()` — most of
each poller's actual substance — stay separate. But both decide "is this
person due, and how urgently" by the exact same rules, and used to say so
twice, byte-for-byte except for which dataclass's fields it read. Free
functions over primitives here, not a shared base class: a base class would
mostly hide two override points (`_interval`'s online/in-game check) behind
ceremony for what is otherwise three genuinely identical calculations.
"""

from __future__ import annotations

from bot.util import parse_iso, utcnow

# Sparser polling of absent people is politeness, not thrift: neither
# provider's budget is anywhere near a constraint (SPEC 5.2).
IDLE_AFTER_SECONDS = 2 * 3600

# Both tickers fire every 60s, but a timestamp is written a moment after the
# tick begins, so the next tick measures 59.x seconds and decides it is too
# early. Every interval would then silently double: presence once in two
# minutes, achievements once in four. The tolerance must be smaller than the
# tick.
DUE_TOLERANCE_SECONDS = 5


def is_due(last_updated_at: str | None, interval_seconds: int) -> bool:
    """Has it been at least `interval_seconds` (minus the tick-alignment
    tolerance above) since the target's presence row was last touched?"""
    last = parse_iso(last_updated_at)
    if last is None:
        return True
    return (utcnow() - last).total_seconds() >= interval_seconds - DUE_TOLERANCE_SECONDS


def debounce_passed(last_poll_at: str | None, poll_interval_seconds: int) -> bool:
    """No more than one achievement request per game within
    `poll_interval_seconds` (SPEC 5.3), even if several events fire in a
    row — same rule, same constant, on both platforms."""
    last = parse_iso(last_poll_at)
    if last is None:
        return True
    return (utcnow() - last).total_seconds() >= poll_interval_seconds - DUE_TOLERANCE_SECONDS


def presence_interval(
    *,
    online: bool,
    in_game: bool,
    changed_at: str | None,
    interval_in_game: int,
    interval_online: int,
    interval_offline: int,
    interval_idle: int,
) -> int:
    """How long to wait before the next presence check, given the target's
    last known state. `online`/`in_game` are the caller's own translation of
    its platform's state shape — everything past that point is identical."""
    if online:
        return interval_in_game if in_game else interval_online
    changed = parse_iso(changed_at)
    offline_for = (utcnow() - changed).total_seconds() if changed else 0.0
    if offline_for >= IDLE_AFTER_SECONDS:
        return interval_idle
    return interval_offline
