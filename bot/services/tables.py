"""Shared list rendering for Telegram messages: /stats, /recent, /summary
and the daily itog all show a ranked or ordered list of rows.

Used to be a `<pre>` monospace table with padded columns — dropped: it reads
as a code block (grey background, wraps badly on a narrow phone screen), not
as a leaderboard or a list of games. A `<blockquote expandable>` of plain
lines is the Telegram-native shape for "here's a report section, tap to see
the rest" (SPEC 6.3, 7.3) — no column alignment attempted, each row is just
a readable sentence.
"""

from __future__ import annotations

# A long name doesn't get cut off gracefully — it wraps the whole line onto a
# second one on a phone screen (found live: a game called "Minecraft Legends
# - Windows" did exactly this in the old table). Generous compared to the old
# table's 20: a plain line has room a rigid column didn't.
NAME_LIMIT = 28


def truncate_name(name: str, limit: int = NAME_LIMIT) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def blockquote(rows: list[str], *, expandable: bool = True) -> str:
    """Wraps already-built, already-HTML-escaped lines in a Telegram
    blockquote. `expandable=False` for a view reached by an explicit
    "показать всех" tap (SPEC 6.3) — collapsing it again would undo the
    point of asking for the uncapped list.
    """
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return tag + "\n".join(rows) + "</blockquote>"


def total_line(label: str, text: str) -> str:
    """The one line every list's summary starts with, highlighted the same
    way everywhere so it reads as a total, not another row (now placed
    *before* the list it summarizes, not after — SPEC 6.3, 7.3)."""
    return f"<b>{label}:</b> {text}"
