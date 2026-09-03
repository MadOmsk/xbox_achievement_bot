"""Shared monospace table rendering, used by /stats, /top and the summary."""

from __future__ import annotations

from bot.services.tables import render_table, total_line, truncate_name


def test_columns_line_up_across_rows() -> None:
    """Every line must be the same length, header and divider included — that
    is the entire point of a monospace table: same offset, every row."""
    table = render_table(
        ["Игрок", "Ач."],
        [["Igor", "1"], ["MadOmskLongName", "12"]],
        ["<", ">"],
    )
    body = table.removeprefix("<pre>").removesuffix("</pre>")
    lines = body.splitlines()
    assert len(lines) == 4  # header + divider + two rows
    assert len({len(line) for line in lines}) == 1


def test_vertical_bar_separates_columns() -> None:
    """Plain space-padded columns read as a run-on string — nothing marks
    where one column ends and the next begins."""
    table = render_table(["A", "B"], [["1", "2"]], ["<", "<"])
    assert "│" in table


def test_html_is_escaped_after_padding_not_before() -> None:
    """Padding is computed on the raw string: an HTML entity like &amp; still
    occupies one visual column once Telegram's parser collapses it back to a
    single character, so escaping first would throw the alignment off."""
    table = render_table(["Игрок"], [["A&B"], ["C"]], ["<"])
    assert "&amp;" in table
    assert "<B>" not in table  # never becomes a real (bogus) tag


def test_no_rows_still_renders_the_header() -> None:
    table = render_table(["#", "Игрок"], [], ["<", "<"])
    assert table.startswith("<pre># │ Игрок\n")
    assert table.endswith("</pre>")


def test_total_line_is_bold_and_marked() -> None:
    line = total_line("Всего", "3 ачивки, +100 G")
    assert "<b>Всего:</b>" in line
    assert "3 ачивки, +100 G" in line


def test_truncate_name_leaves_short_names_alone() -> None:
    assert truncate_name("Igor") == "Igor"
    assert truncate_name("A" * 20, limit=20) == "A" * 20  # exactly at the limit


def test_truncate_name_cuts_and_marks_long_ones() -> None:
    # "Minecraft Legends - Windows" wrapping onto a second line and breaking
    # the whole table's alignment is the bug this exists to prevent.
    truncated = truncate_name("Minecraft Legends - Windows", limit=20)
    assert len(truncated) == 20
    assert truncated.endswith("…")
    assert truncated.startswith("Minecraft Legends")
