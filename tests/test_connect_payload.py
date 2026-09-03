"""Deep-link payload parsing for the group hub's «Подключить Xbox» button
(SPEC 6.3)."""

from __future__ import annotations

from bot.handlers.connect import _parse_connect_payload


def test_plain_connect_has_no_origin_chat() -> None:
    assert _parse_connect_payload("connect") == (True, None)


def test_connect_with_a_group_id_extracts_it() -> None:
    # Group chat ids are always negative.
    assert _parse_connect_payload("connect-1001234567890") == (True, -1001234567890)


def test_unrelated_payload_is_not_a_connect_payload() -> None:
    assert _parse_connect_payload("panel") == (False, None)
    assert _parse_connect_payload("") == (False, None)


def test_garbage_after_connect_does_not_crash() -> None:
    assert _parse_connect_payload("connectnonsense") == (False, None)
