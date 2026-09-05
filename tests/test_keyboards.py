"""Panel keyboard construction (SPEC 6.2)."""

from __future__ import annotations

from bot.handlers.keyboards import (
    next_rarity_mode,
    panel_keyboard,
    steam_profile_url,
    xbox_profile_url,
)


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


def test_not_connected_keyboard_offers_steam_disconnect_once_connected() -> None:
    """Steam-only, no XBOX at all — still gets a real disconnect option for
    the platform it does have, not nothing (2026-09-05 follow-up)."""
    markup = panel_keyboard(None, connected=False, steam_connected=True)
    assert _callback_data(markup) == ["relogin", "steam:disconnectprompt"]


def test_connected_keyboard_offers_steam_connect_or_disconnect_not_both() -> None:
    connect_only = panel_keyboard(180, connected=True)
    assert "steam:connect" in _callback_data(connect_only)
    assert "steam:disconnectprompt" not in _callback_data(connect_only)

    disconnect_only = panel_keyboard(180, connected=True, steam_connected=True)
    assert "steam:disconnectprompt" in _callback_data(disconnect_only)
    assert "steam:connect" not in _callback_data(disconnect_only)


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


def _disconnect_row(markup, callback_data: str) -> list:
    return next(
        row for row in markup.inline_keyboard if callback_data in [b.callback_data for b in row]
    )


def test_xbox_disconnect_row_gains_a_profile_link_when_gamertag_is_known() -> None:
    """2026-09-05 follow-up: profile link and disconnect share one row."""
    without = panel_keyboard(180, connected=True)
    row = _disconnect_row(without, "panel:disconnect")
    assert len(row) == 1  # no gamertag given — no profile button to add

    with_tag = panel_keyboard(180, connected=True, gamertag="Mad Omsk")
    row = _disconnect_row(with_tag, "panel:disconnect")
    assert len(row) == 2
    assert row[0].url == xbox_profile_url("Mad Omsk")
    assert row[1].callback_data == "panel:disconnect"


def test_steam_disconnect_row_gains_a_profile_link_when_steam_id_is_known() -> None:
    without = panel_keyboard(180, connected=True, steam_connected=True)
    row = _disconnect_row(without, "steam:disconnectprompt")
    assert len(row) == 1

    with_id = panel_keyboard(
        180, connected=True, steam_connected=True, steam_id="76561197960287930"
    )
    row = _disconnect_row(with_id, "steam:disconnectprompt")
    assert len(row) == 2
    assert row[0].url == steam_profile_url("76561197960287930")
    assert row[1].callback_data == "steam:disconnectprompt"


def test_xbox_profile_url_encodes_the_gamertag() -> None:
    assert xbox_profile_url("Mad Omsk") == (
        "https://account.xbox.com/en-us/profile?gamertag=Mad%20Omsk"
    )


def test_steam_profile_url_uses_the_steamid64() -> None:
    assert (
        steam_profile_url("76561197960287930")
        == "https://steamcommunity.com/profiles/76561197960287930"
    )
