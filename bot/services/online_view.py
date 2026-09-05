"""Rendering /online's table (SPEC 6.3) — shared by the command itself
(handlers/chat.py) and the auto-refresh poller (poller/online_refresh.py,
Follow-up 2026-09-05), so a live-updating table looks exactly like the one
/online posts fresh. Split out of handlers/chat.py so the poller (which must
not import from handlers — services are the one layer both may depend on)
can build the same text.
"""

from __future__ import annotations

from bot.db.repo import ChatPresenceRow
from bot.services.achievements import PLATFORM_ICON


def presence_text(row: ChatPresenceRow) -> str:
    if row.state == "Online" and row.title_id:
        return f"играет — {row.title_name or row.title_id}"
    if row.state == "Online":
        return "в сети, не играет"
    if row.state is not None:
        return "не в сети"
    return "нет данных"


def presence_icon(row: ChatPresenceRow) -> str:
    # Platform colour while online (SPEC 9, M-Steam-2e) — grey for
    # offline/no data regardless of platform. Found live: a pure platform
    # colour made every offline row look the same as an online one at a
    # glance, losing the one signal a colour is actually good for.
    if row.state != "Online":
        return "⚪"
    return PLATFORM_ICON.get(row.platform, "⚪")


def render_online_table(rows: list[ChatPresenceRow], updated_label: str) -> str:
    """`updated_label` is a ready-made "HH:MM" in the chat's own timezone
    (Follow-up 2026-09-05, the "Обновлено: …" line) — this module has no
    idea what timezone a chat is in, that's services/stats.py's
    local_now()'s job, done by the caller (handlers/chat.py,
    poller/online_refresh.py alike)."""
    lines = ["🎮 <b>Онлайн-статус игроков</b>", f"<i>Обновлено: {updated_label}</i>", ""]
    for row in rows:
        name = row.gamertag or f"id{row.tg_id}"
        lines.append(f"{presence_icon(row)} {name} — {presence_text(row)}")
    return "\n".join(lines)
