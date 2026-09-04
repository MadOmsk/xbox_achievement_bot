"""_current_title (SPEC 5.2, 5.3): picking the game someone is actually
playing out of presence's device/title list, not Microsoft's own shell or
app entries mixed in alongside it."""

from __future__ import annotations

from types import SimpleNamespace

from bot.services.xbox.client import _current_title


def _title(id: int, name: str, placement: str = "Full", state: str = "Active"):
    return SimpleNamespace(id=id, name=name, placement=placement, state=state)


def _device(titles: list, type_: str = "WindowsOneCore"):
    return SimpleNamespace(type=type_, titles=titles)


def _item(devices: list):
    return SimpleNamespace(devices=devices)


def test_picks_the_active_full_placement_title() -> None:
    item = _item([_device([_title(111, "Halo Infinite")])])
    assert _current_title(item) == ("111", "Halo Infinite", "WindowsOneCore")


def test_skips_the_dashboard() -> None:
    """"Home" — the existing, already-shipped exclusion."""
    item = _item([_device([_title(1, "Home"), _title(111, "Halo Infinite")])])
    assert _current_title(item) == ("111", "Halo Infinite", "WindowsOneCore")


def test_skips_the_xbox_app_itself() -> None:
    """Found live: title_id 704208617 named "XBOX" resolves via titlehub to
    0 max_gamerscore and 0 achievements — the Xbox app, not a game — and
    without this exclusion it fed straight into /online showing "играет —
    XBOX" for someone who wasn't actually playing anything (SPEC 9,
    M-Steam-2e's activity-level merge trusted this as real "playing")."""
    item = _item([_device([_title(704208617, "XBOX")])])
    assert _current_title(item) == (None, None, None)


def test_xbox_app_exclusion_is_case_insensitive() -> None:
    item = _item([_device([_title(704208617, "Xbox")])])
    assert _current_title(item) == (None, None, None)


def test_skips_non_full_or_non_active_titles() -> None:
    item = _item(
        [
            _device(
                [
                    _title(1, "Snapped Game", placement="Fill"),
                    _title(2, "Suspended Game", state="Suspended"),
                    _title(111, "Halo Infinite"),
                ]
            )
        ]
    )
    assert _current_title(item) == ("111", "Halo Infinite", "WindowsOneCore")


def test_no_qualifying_title_returns_all_none() -> None:
    item = _item([_device([_title(1, "Home")])])
    assert _current_title(item) == (None, None, None)


def test_no_devices_at_all_returns_all_none() -> None:
    assert _current_title(SimpleNamespace(devices=None)) == (None, None, None)
