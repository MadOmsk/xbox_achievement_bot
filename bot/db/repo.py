"""Data access. No SQL lives anywhere else in the project (CLAUDE.md).

Timestamps are UTC ISO strings; conversion to a person's timezone happens at
display time, never here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Self

import aiosqlite

from bot.util import utcnow, utcnow_iso

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

DEFAULT_APP_SETTINGS: dict[str, str] = {
    "rare_threshold_percent": "10",
    "daily_summary_time": "23:00",
}


@dataclass(slots=True)
class User:
    tg_id: int
    username: str | None
    xuid: str | None
    gamertag: str | None
    gamerscore: int | None
    is_excluded: bool
    last_online_at: str | None


@dataclass(slots=True)
class TokenRecord:
    tg_id: int
    refresh_token_enc: bytes
    status: str
    fail_count: int
    last_refresh_at: str | None
    invalid_at: str | None
    notify_count: int
    last_notified_at: str | None


@dataclass(slots=True)
class UserSettings:
    tg_id: int
    rarity_mode: str
    show_x360: bool
    digest_threshold: int
    tz_offset_min: int | None


@dataclass(slots=True)
class PollTarget:
    """A user the poller may look at, with his last known presence."""

    tg_id: int
    xuid: str
    state: str | None
    title_id: str | None
    title_name: str | None
    changed_at: str | None
    last_ach_poll_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class AchievementRow:
    title_id: str
    achievement_id: str
    name: str
    description: str | None
    icon_url: str | None
    unlocked_at: str | None
    gamerscore: int
    rarity_percent: float | None
    platform: str
    title_name: str | None = None


@dataclass(slots=True)
class ChatTarget:
    chat_id: int
    title: str | None
    rarity_mode: str
    min_gamerscore: int
    muted_title_ids: list[str]


@dataclass(slots=True)
class TitleHistoryRow:
    title_id: str
    name: str
    platform: str
    current_gamerscore: int | None
    max_gamerscore: int | None
    achievements_unlocked: int | None
    achievements_total: int | None
    last_played_at: str | None


class Database:
    """Owns the connection and brings the file up to the current schema."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def connect(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        # WAL keeps the poller writing while a panel reads; foreign keys are off
        # by default in SQLite and our ON DELETE CASCADE depends on them.
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._apply_schema()
        await self._apply_migrations()
        await self._seed_app_settings()
        await self._conn.commit()
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _apply_schema(self) -> None:
        await self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    async def _apply_migrations(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL)"
        )
        cursor = await self.conn.execute("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in await cursor.fetchall()}

        if not MIGRATIONS_DIR.exists():
            return
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in applied:
                continue
            log.info("applying migration %s", path.stem)
            await self.conn.executescript(path.read_text(encoding="utf-8"))
            await self.conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (path.stem, utcnow_iso()),
            )

    async def _seed_app_settings(self) -> None:
        for key, value in DEFAULT_APP_SETTINGS.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, utcnow_iso()),
            )


class Repo:
    """Every query in the project. Services call these; handlers call services."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    # ---------------------------------------------------------------- users

    async def ensure_user(self, tg_id: int, username: str | None = None) -> None:
        """Create the user and his settings row on first contact."""
        now = utcnow_iso()
        await self._conn.execute(
            "INSERT INTO users (tg_id, username, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET "
            "  username = COALESCE(excluded.username, users.username),"
            "  updated_at = excluded.updated_at",
            (tg_id, username, now, now),
        )
        await self._conn.execute("INSERT OR IGNORE INTO user_settings (tg_id) VALUES (?)", (tg_id,))
        await self._conn.commit()

    async def update_username(self, tg_id: int, username: str) -> None:
        await self._conn.execute(
            "UPDATE users SET username = ? WHERE tg_id = ? AND IFNULL(username, '') <> ?",
            (username, tg_id, username),
        )
        await self._conn.commit()

    async def get_user(self, tg_id: int) -> User | None:
        cursor = await self._conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return _as_user(row) if row else None

    async def get_user_by_xuid(self, xuid: str) -> User | None:
        cursor = await self._conn.execute("SELECT * FROM users WHERE xuid = ?", (xuid,))
        row = await cursor.fetchone()
        return _as_user(row) if row else None

    async def link_xbox_account(
        self, tg_id: int, xuid: str, gamertag: str | None, gamerscore: int | None
    ) -> None:
        await self._conn.execute(
            "UPDATE users SET xuid = ?, gamertag = ?, gamerscore = ?, updated_at = ? "
            "WHERE tg_id = ?",
            (xuid, gamertag, gamerscore, utcnow_iso(), tg_id),
        )
        await self._conn.commit()

    async def unlink_xbox_account(self, tg_id: int) -> None:
        """/disconnect: the link goes, seen_achievements and history stay (SPEC 6.1)."""
        await self._conn.execute(
            "UPDATE users SET xuid = NULL, updated_at = ? WHERE tg_id = ?",
            (utcnow_iso(), tg_id),
        )
        await self._conn.commit()

    # --------------------------------------------------------------- tokens

    async def save_refresh_token(self, tg_id: int, token_enc: bytes) -> None:
        """Store a fresh token and clear the failure state.

        Called both on first connect and on every refresh — SPEC 5.1 requires
        the new token to reach the database *before* the request that uses it.
        """
        now = utcnow_iso()
        await self._conn.execute(
            "INSERT INTO tokens (tg_id, refresh_token_enc, status, created_at, last_refresh_at) "
            "VALUES (?, ?, 'active', ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET "
            "  refresh_token_enc = excluded.refresh_token_enc,"
            "  status = 'active',"
            "  fail_count = 0,"
            "  invalid_at = NULL,"
            "  notify_count = 0,"
            "  last_notified_at = NULL,"
            "  last_refresh_at = excluded.last_refresh_at",
            (tg_id, token_enc, now, now),
        )
        await self._conn.commit()

    async def get_token(self, tg_id: int) -> TokenRecord | None:
        cursor = await self._conn.execute("SELECT * FROM tokens WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return _as_token(row) if row else None

    async def set_token_status(self, tg_id: int, status: str) -> None:
        invalid_at = utcnow_iso() if status == "invalid" else None
        await self._conn.execute(
            "UPDATE tokens SET status = ?, invalid_at = ? WHERE tg_id = ?",
            (status, invalid_at, tg_id),
        )
        await self._conn.commit()

    async def bump_token_failure(self, tg_id: int) -> int:
        """A network error is not a dead token (SPEC 5.1) — count and report."""
        await self._conn.execute(
            "UPDATE tokens SET fail_count = fail_count + 1 WHERE tg_id = ?", (tg_id,)
        )
        await self._conn.commit()
        cursor = await self._conn.execute("SELECT fail_count FROM tokens WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return int(row["fail_count"]) if row else 0

    async def tokens_needing_reminder(
        self, max_reminders: int, min_interval_hours: int
    ) -> list[int]:
        """Who to remind about a dead login (SPEC 5.1.1).

        Capped at `max_reminders`: the person may have left on purpose, and a
        bot that nags for months is a bot that gets blocked.
        """
        cutoff = (utcnow() - timedelta(hours=min_interval_hours)).isoformat(timespec="seconds")
        cursor = await self._conn.execute(
            "SELECT tg_id FROM tokens "
            "WHERE status = 'invalid' AND notify_count < ? "
            "  AND (last_notified_at IS NULL OR last_notified_at < ?)",
            (max_reminders, cutoff),
        )
        return [row["tg_id"] for row in await cursor.fetchall()]

    async def mark_token_notified(self, tg_id: int) -> None:
        await self._conn.execute(
            "UPDATE tokens SET notify_count = notify_count + 1, last_notified_at = ? "
            "WHERE tg_id = ?",
            (utcnow_iso(), tg_id),
        )
        await self._conn.commit()

    async def delete_token(self, tg_id: int) -> None:
        await self._conn.execute("DELETE FROM tokens WHERE tg_id = ?", (tg_id,))
        await self._conn.commit()

    # ------------------------------------------------------- user settings

    async def get_user_settings(self, tg_id: int) -> UserSettings | None:
        cursor = await self._conn.execute("SELECT * FROM user_settings WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return _as_user_settings(row) if row else None

    async def update_user_settings(self, tg_id: int, **fields: Any) -> None:
        allowed = {"rarity_mode", "show_x360", "digest_threshold", "tz_offset_min"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown user_settings fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._conn.execute(
            f"UPDATE user_settings SET {assignments} WHERE tg_id = ?",
            (*fields.values(), tg_id),
        )
        await self._conn.commit()

    # --------------------------------------------------------- app settings

    async def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        cursor = await self._conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_app_setting(self, key: str, value: str, updated_by: int | None = None) -> None:
        await self._conn.execute(
            "INSERT INTO app_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value = excluded.value, updated_by = excluded.updated_by,"
            "  updated_at = excluded.updated_at",
            (key, value, updated_by, utcnow_iso()),
        )
        await self._conn.commit()

    # -------------------------------------------------------- subscriptions

    async def delete_subscriptions_of_user(self, tg_id: int) -> None:
        await self._conn.execute("DELETE FROM subscriptions WHERE tg_id = ?", (tg_id,))
        await self._conn.commit()

    async def delete_presence_state(self, xuid: str) -> None:
        await self._conn.execute("DELETE FROM presence_state WHERE xuid = ?", (xuid,))
        await self._conn.commit()

    async def chats_of_user(self, tg_id: int) -> list[str]:
        cursor = await self._conn.execute(
            "SELECT c.title FROM subscriptions s JOIN chats c ON c.chat_id = s.chat_id "
            "WHERE s.tg_id = ? AND c.is_active = 1",
            (tg_id,),
        )
        return [row["title"] or "без названия" for row in await cursor.fetchall()]

    # ------------------------------------------------------------- polling

    async def pollable_users(self) -> list[PollTarget]:
        """Who the poller is allowed to touch (SPEC 5.5, 6.4).

        Excluded users and dead tokens are filtered out here rather than in the
        poller: every tick for them would be a guaranteed failure.
        """
        cursor = await self._conn.execute(
            "SELECT u.tg_id, u.xuid, p.state, p.title_id, p.title_name,"
            "       p.changed_at, p.last_ach_poll_at, p.updated_at "
            "FROM users u "
            "JOIN tokens t ON t.tg_id = u.tg_id "
            "LEFT JOIN presence_state p ON p.xuid = u.xuid "
            "WHERE u.xuid IS NOT NULL AND u.is_excluded = 0 AND t.status = 'active'"
        )
        return [
            PollTarget(
                tg_id=row["tg_id"],
                xuid=row["xuid"],
                state=row["state"],
                title_id=row["title_id"],
                title_name=row["title_name"],
                changed_at=row["changed_at"],
                last_ach_poll_at=row["last_ach_poll_at"],
                updated_at=row["updated_at"],
            )
            for row in await cursor.fetchall()
        ]

    async def save_presence_state(
        self,
        xuid: str,
        state: str,
        title_id: str | None,
        title_name: str | None,
        *,
        changed: bool,
    ) -> None:
        now = utcnow_iso()
        await self._conn.execute(
            "INSERT INTO presence_state "
            "(xuid, state, title_id, title_name, changed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(xuid) DO UPDATE SET "
            "  state = excluded.state, title_id = excluded.title_id,"
            "  title_name = excluded.title_name, updated_at = excluded.updated_at,"
            "  changed_at = CASE WHEN ? THEN excluded.changed_at "
            "                 ELSE presence_state.changed_at END",
            (xuid, state, title_id, title_name, now, now, 1 if changed else 0),
        )
        await self._conn.commit()

    async def mark_achievements_polled(self, xuid: str) -> None:
        await self._conn.execute(
            "UPDATE presence_state SET last_ach_poll_at = ? WHERE xuid = ?",
            (utcnow_iso(), xuid),
        )
        await self._conn.commit()

    async def touch_last_online(self, tg_id: int) -> None:
        await self._conn.execute(
            "UPDATE users SET last_online_at = ?, updated_at = ? WHERE tg_id = ?",
            (utcnow_iso(), utcnow_iso(), tg_id),
        )
        await self._conn.commit()

    # -------------------------------------------------------- achievements

    async def insert_new_achievements(
        self, xuid: str, achievements: Sequence[AchievementRow], *, is_backfill: bool
    ) -> list[AchievementRow]:
        """Insert what we have not seen and report back only the new rows.

        The primary key (xuid, title_id, achievement_id) is the deduplication:
        INSERT OR IGNORE tells us which rows were actually new.
        """
        new_rows: list[AchievementRow] = []
        now = utcnow_iso()
        for item in achievements:
            cursor = await self._conn.execute(
                "INSERT OR IGNORE INTO seen_achievements "
                "(xuid, title_id, achievement_id, name, description, icon_url, unlocked_at,"
                " gamerscore, rarity_percent, platform, is_backfill, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    xuid,
                    item.title_id,
                    item.achievement_id,
                    item.name,
                    item.description,
                    item.icon_url,
                    item.unlocked_at,
                    item.gamerscore,
                    item.rarity_percent,
                    item.platform,
                    1 if is_backfill else 0,
                    now,
                ),
            )
            if cursor.rowcount:
                new_rows.append(item)
        await self._conn.commit()
        return new_rows

    async def has_any_achievements(self, xuid: str) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM seen_achievements WHERE xuid = ? LIMIT 1", (xuid,)
        )
        return await cursor.fetchone() is not None

    async def record_publication(
        self, chat_id: int, xuid: str, title_id: str, achievement_id: str, message_id: int | None
    ) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO publications "
            "(chat_id, xuid, title_id, achievement_id, message_id, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, xuid, title_id, achievement_id, message_id, utcnow_iso()),
        )
        await self._conn.commit()

    async def is_published(
        self, chat_id: int, xuid: str, title_id: str, achievement_id: str
    ) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM publications "
            "WHERE chat_id = ? AND xuid = ? AND title_id = ? AND achievement_id = ?",
            (chat_id, xuid, title_id, achievement_id),
        )
        return await cursor.fetchone() is not None

    # --------------------------------------------------- chats and subscriptions

    async def upsert_chat(self, chat_id: int, title: str | None, added_by: int | None) -> None:
        now = utcnow_iso()
        await self._conn.execute(
            "INSERT INTO chats (chat_id, title, added_by, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, is_active = 1",
            (chat_id, title, added_by, now),
        )
        await self._conn.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await self._conn.commit()

    async def subscribe(self, chat_id: int, tg_id: int) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, tg_id, created_at) VALUES (?, ?, ?)",
            (chat_id, tg_id, utcnow_iso()),
        )
        await self._conn.commit()

    async def unsubscribe(self, chat_id: int, tg_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND tg_id = ?", (chat_id, tg_id)
        )
        await self._conn.commit()

    async def is_subscribed(self, chat_id: int, tg_id: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM subscriptions WHERE chat_id = ? AND tg_id = ?", (chat_id, tg_id)
        )
        return await cursor.fetchone() is not None

    async def deactivate_chat(self, chat_id: int) -> None:
        """Telegram answered 403 — the bot was kicked out (SPEC 5.5)."""
        await self._conn.execute("UPDATE chats SET is_active = 0 WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

    async def publication_targets(self, tg_id: int) -> list[ChatTarget]:
        cursor = await self._conn.execute(
            "SELECT c.chat_id, c.title, s.rarity_mode, s.min_gamerscore, s.muted_title_ids "
            "FROM subscriptions sub "
            "JOIN chats c ON c.chat_id = sub.chat_id "
            "JOIN chat_settings s ON s.chat_id = c.chat_id "
            "WHERE sub.tg_id = ? AND c.is_active = 1",
            (tg_id,),
        )
        return [
            ChatTarget(
                chat_id=row["chat_id"],
                title=row["title"],
                rarity_mode=row["rarity_mode"],
                min_gamerscore=row["min_gamerscore"],
                muted_title_ids=json.loads(row["muted_title_ids"] or "[]"),
            )
            for row in await cursor.fetchall()
        ]

    # --------------------------------------------------------- title history

    async def save_title_history(self, xuid: str, entries: Sequence[TitleHistoryRow]) -> None:
        now = utcnow_iso()
        for entry in entries:
            await self._conn.execute(
                "INSERT INTO title_history (xuid, title_id, current_gamerscore, max_gamerscore,"
                " achievements_unlocked, achievements_total, last_played_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(xuid, title_id) DO UPDATE SET "
                "  current_gamerscore = excluded.current_gamerscore,"
                "  max_gamerscore = excluded.max_gamerscore,"
                "  achievements_unlocked = excluded.achievements_unlocked,"
                "  achievements_total = excluded.achievements_total,"
                "  last_played_at = excluded.last_played_at,"
                "  updated_at = excluded.updated_at",
                (
                    xuid,
                    entry.title_id,
                    entry.current_gamerscore,
                    entry.max_gamerscore,
                    entry.achievements_unlocked,
                    entry.achievements_total,
                    entry.last_played_at,
                    now,
                ),
            )
            await self._conn.execute(
                "INSERT INTO titles (title_id, name, platform, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(title_id) DO UPDATE SET name = excluded.name,"
                " platform = excluded.platform, updated_at = excluded.updated_at",
                (entry.title_id, entry.name, entry.platform, now),
            )
        await self._conn.commit()

    async def update_gamerscore(self, tg_id: int, gamerscore: int) -> None:
        await self._conn.execute(
            "UPDATE users SET gamerscore = ?, updated_at = ? WHERE tg_id = ?",
            (gamerscore, utcnow_iso(), tg_id),
        )
        await self._conn.commit()

    async def title_name(self, title_id: str) -> str | None:
        cursor = await self._conn.execute("SELECT name FROM titles WHERE title_id = ?", (title_id,))
        row = await cursor.fetchone()
        return row["name"] if row else None


def _as_user(row: aiosqlite.Row) -> User:
    return User(
        tg_id=row["tg_id"],
        username=row["username"],
        xuid=row["xuid"],
        gamertag=row["gamertag"],
        gamerscore=row["gamerscore"],
        is_excluded=bool(row["is_excluded"]),
        last_online_at=row["last_online_at"],
    )


def _as_token(row: aiosqlite.Row) -> TokenRecord:
    return TokenRecord(
        tg_id=row["tg_id"],
        refresh_token_enc=row["refresh_token_enc"],
        status=row["status"],
        fail_count=row["fail_count"],
        last_refresh_at=row["last_refresh_at"],
        invalid_at=row["invalid_at"],
        notify_count=row["notify_count"],
        last_notified_at=row["last_notified_at"],
    )


def _as_user_settings(row: aiosqlite.Row) -> UserSettings:
    return UserSettings(
        tg_id=row["tg_id"],
        rarity_mode=row["rarity_mode"],
        show_x360=bool(row["show_x360"]),
        digest_threshold=row["digest_threshold"],
        tz_offset_min=row["tz_offset_min"],
    )
