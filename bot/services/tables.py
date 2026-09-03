"""Shared monospace table rendering for Telegram messages.

One implementation so the daily/summary report, /stats and /top all look the
same — a future style change touches this file, not every command that
happens to print numbers.

Telegram has no literal table markup; an HTML `<pre>` block with padded
columns is the only way the numbers actually line up under its monospace font.
"""

from __future__ import annotations

from html import escape as html_escape

Align = str  # "<" (left) or ">" (right)

# A long name doesn't get cut off gracefully — it wraps the whole row onto a
# second line on a phone screen, which breaks the "columns line up" promise
# a monospace table exists for in the first place (found live: a game called
# "Minecraft Legends - Windows" wrapped its "+G" cell onto its own line).
# 20 keeps the widest realistic table (4-5 columns) inside a phone width.
NAME_LIMIT = 20


def truncate_name(name: str, limit: int = NAME_LIMIT) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def render_table(headers: list[str], rows: list[list[str]], aligns: list[Align]) -> str:
    """A `<pre>` block with a header row, a divider and padded columns
    separated by a vertical bar — plain space-padded columns read as a run-on
    string with nothing marking where one ends and the next begins.

    Escaped once, on the finished block, not cell by cell: an HTML entity like
    `&amp;` still occupies exactly one visual column once Telegram's parser
    collapses it back to a single character, so escaping after padding keeps
    the alignment intact.
    """
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def render_row(cells: list[str]) -> str:
        parts = [
            cell.ljust(width) if align == "<" else cell.rjust(width)
            for cell, width, align in zip(cells, widths, aligns, strict=True)
        ]
        return " │ ".join(parts).rstrip()

    divider = "─┼─".join("─" * width for width in widths)
    lines = [render_row(headers), divider, *(render_row(row) for row in rows)]
    return "<pre>" + html_escape("\n".join(lines)) + "</pre>"


def total_line(label: str, text: str) -> str:
    """The one line every table-plus-total block ends with, highlighted the
    same way everywhere so it reads as a total, not another table row."""
    return f"<b>{label}:</b> {text}"
