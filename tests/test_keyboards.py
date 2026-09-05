"""Panel keyboard construction (SPEC 6.2)."""

from __future__ import annotations

from bot.handlers.keyboards import next_rarity_mode, panel_keyboard


def _button_texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _callback_data(markup) -> list[str | None]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_rarity_cycles_through_all_three_and_wraps() -> None:
    assert next_rarity_mode("all") == "rare"
    assert next_rarity_mode("rare") == "hidden"
    assert next_rarity_mode("hidden") == "all"


def test_unknown_rarity_mode_starts_the_cycle_over() -> None:
    # Defensive: a row that somehow holds neither of the three values must
    # not raise — it just resets to the first choice.
    assert next_rarity_mode("whatever") == "rare"


def test_not_connected_keyboard_offers_both_platforms() -> None:
    """Xbox and Steam are independent (M-Steam-1) — someone with neither
    connected should be offered both, not just Xbox (2026-09-05 follow-up)."""
    markup = panel_keyboard(None, connected=False)
    assert _callback_data(markup) == ["relogin", "steam:connect"]


def test_not_connected_keyboard_hides_steam_button_once_connected() -> None:
    markup = panel_keyboard(None, connected=False, steam_connected=True)
    assert _callback_data(markup) == ["relogin"]


def test_connected_keyboard_offers_steam_only_until_connected() -> None:
    assert "steam:connect" in _callback_data(panel_keyboard(180, connected=True))
    connected_both = panel_keyboard(180, connected=True, steam_connected=True)
    assert "steam:connect" not in _callback_data(connected_both)


def test_needs_reconnect_adds_a_button_without_hiding_settings() -> None:
    connected = panel_keyboard(None, connected=True, needs_reconnect=False)
    reconnecting = panel_keyboard(None, connected=True, needs_reconnect=True)

    assert "relogin" not in _callback_data(connected)
    data = _callback_data(reconnecting)
    assert data[0] == "relogin"  # up front, not buried under settings
    assert "panel:tz" in data  # settings still reachable, not replaced


def test_connected_keyboard_offers_disconnect() -> None:
    markup = panel_keyboard(180, connected=True)
    assert "panel:disconnect" in _callback_data(markup)
