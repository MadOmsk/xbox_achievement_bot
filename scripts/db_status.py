"""One-glance summary of the database, for `manage.ps1 status`.

Read-only and dependency-free on purpose: it must work while the bot is down.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "bot.db"


def scalar(conn: sqlite3.Connection, query: str) -> object:
    row = conn.execute(query).fetchone()
    return row[0] if row else None


def main() -> int:
    if not DB_PATH.exists():
        print("База ещё не создана.")
        return 0

    # read-only: the running bot must not be disturbed by a status check
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        users = scalar(conn, "SELECT COUNT(*) FROM users WHERE xuid IS NOT NULL")
        active = scalar(conn, "SELECT COUNT(*) FROM tokens WHERE status = 'active'")
        dead = scalar(conn, "SELECT COUNT(*) FROM tokens WHERE status <> 'active'")
        chats = scalar(conn, "SELECT COUNT(*) FROM chats WHERE is_active = 1")
        subs = scalar(conn, "SELECT COUNT(*) FROM subscriptions")
        seen = scalar(conn, "SELECT COUNT(*) FROM seen_achievements")
        published = scalar(conn, "SELECT COUNT(*) FROM publications")
        last_poll = scalar(conn, "SELECT MAX(updated_at) FROM presence_state")
        today = scalar(
            conn,
            "SELECT COUNT(*) FROM seen_achievements "
            "WHERE unlocked_at >= date('now') AND is_backfill = 0",
        )
    finally:
        conn.close()

    print("База:")
    print(f"  подключено:    {users} (токенов живых {active}, мёртвых {dead})")
    print(f"  чатов:         {chats}, подписок {subs}")
    print(f"  ачивок:        {seen}, опубликовано {published}, сегодня новых {today}")
    print(f"  последний тик: {last_poll or 'ещё не было'}{_age(last_poll)}")
    return 0


def _age(timestamp: object) -> str:
    if not isinstance(timestamp, str):
        return ""
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    minutes = int((datetime.now(UTC) - parsed).total_seconds() // 60)
    return f"  ({minutes} мин назад)" if minutes else "  (только что)"


if __name__ == "__main__":
    sys.exit(main())
