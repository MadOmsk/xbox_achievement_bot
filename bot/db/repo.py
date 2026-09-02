"""Data access. No SQL lives anywhere else in the project (CLAUDE.md).

Timestamps are UTC ISO strings; conversion to a person's timezone happens at
display time, never here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import aiosqlite

from bot.util import utcnow_iso

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
