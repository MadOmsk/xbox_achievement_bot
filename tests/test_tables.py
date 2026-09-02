"""Shared monospace table rendering, used by /stats, /top and the summary."""

from __future__ import annotations

from bot.services.tables import render_table, total_line


def test_columns_line_up_across_rows() -> None:
    """Every line must be the same length, header included — that is the
    entire point of a monospace table: same offset, every column, every row."""
    table = render_table(
        ["Игрок", "Ач."],
        [["Igor", "1"], ["MadOmskLongName", "12"]],
        ["<", ">"],
    )
    body = table.removeprefix("<pre>").removesuffix("</pre>")
    lines = body.splitlines()
    assert len(lines) == 3  # header + two rows
    assert len({len(line) for line in lines}) == 1


def test_html_is_escaped_after_padding_not_before() -> None:
    """Padding is computed on the raw string: an HTML entity like &amp; still
    occupies one visual column once Telegram's parser collapses it back to a
    single character, so escaping first would throw the alignment off."""
    table = render_table(["Игрок"], [["A&B"], ["C"]], ["<"])
    assert "&amp;" in table
    assert "<B>" not in table  # never becomes a real (bogus) tag


def test_no_rows_still_renders_the_header() -> None:
    assert render_table(["#", "Игрок"], [], ["<", "<"]) == "<pre># Игрок</pre>"


def test_total_line_is_bold_and_marked() -> None:
    line = total_line("Всего", "3 ачивки, +100 G")
    assert "<b>Всего:</b>" in line
    assert "3 ачивки, +100 G" in line
