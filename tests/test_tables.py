"""Shared list rendering used by /stats, /recent and /summary."""

from __future__ import annotations

from bot.services.tables import blockquote, total_line, truncate_name


def test_blockquote_wraps_rows_expandable_by_default() -> None:
    text = blockquote(["one", "two"])
    assert text == "<blockquote expandable>one\ntwo</blockquote>"


def test_blockquote_can_be_non_expandable() -> None:
    """Used for an explicit "показать всех" tap (SPEC 6.3) — already asked
    for everything, collapsing it again would defeat the point."""
    text = blockquote(["one", "two"], expandable=False)
    assert text == "<blockquote>one\ntwo</blockquote>"


def test_total_line_is_bold_and_marked() -> None:
    line = total_line("Всего", "3 ачивки, +100 G")
    assert "<b>Всего:</b>" in line
    assert "3 ачивки, +100 G" in line


def test_truncate_name_leaves_short_names_alone() -> None:
    assert truncate_name("Igor") == "Igor"
    assert truncate_name("A" * 20, limit=20) == "A" * 20  # exactly at the limit


def test_truncate_name_cuts_and_marks_long_ones() -> None:
    truncated = truncate_name("Minecraft Legends - Windows and DLC bundle", limit=20)
    assert len(truncated) == 20
    assert truncated.endswith("…")
    assert truncated.startswith("Minecraft Legends")
