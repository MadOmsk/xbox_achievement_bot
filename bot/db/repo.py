"""Data access. No SQL lives anywhere else in the project (CLAUDE.md).

Timestamps are UTC ISO strings; conversion to a person's timezone happens at
display time, never here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import aiosqlite

from bot.util import utcnow, utcnow_iso

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

DEFAULT_APP_SETTINGS: dict[str, str] = {
    # Row caps for the game/player tables (SPEC 6.3, 6.6, 7.2) — separate
    # settings because they cap different things: players in /summary's
    # leaderboard, games in /stats' "Игры за 30 дней".
    "summary_top_limit": "15",
    "stats_games_limit": "15",
    # /hltb's own two: how many candidates search() and the recent-games
    # shortcuts pool from, and how many of them show per page (6.4, 6.6).
    "hltb_results_limit": "20",
    "hltb_page_size": "5",
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
    min_gamerscore: int
    muted_title_ids: list[str]
    # Always explicit per chat, no shared fallback (SPEC 5.5, 5.7) — every
    # chat gets a real value the moment it's created.
    rare_threshold_percent: float
    daily_summary_time: str
    tz_offset_min: int
    # Filled in by the admin panel only; the publisher never looks at them.
    is_active: bool = True
    daily_summary: bool = True
    subscribers: int = 0


@dataclass(slots=True)
class ChatDailySettings:
    """The same three per-chat values as on `ChatTarget`, fetched alone for
    call sites (chat.py's /summary) that have a chat_id but no reason to pull
    the rest of the chat/subscriber JOIN."""

    rare_threshold_percent: float
    daily_summary_time: str
    tz_offset_min: int


@dataclass(slots=True)
class AdminUserRow:
    tg_id: int
    gamertag: str | None
    username: str | None
    xuid: str
    gamerscore: int | None
    is_excluded: bool
    last_online_at: str | None
    token_status: str | None
    last_refresh_at: str | None


@dataclass(slots=True)
class PresenceRow:
    xuid: str
    state: str | None
    title_id: str | None
    title_name: str | None
    updated_at: str | None


@dataclass(slots=True)
class ChatPresenceRow:
    """One row of /online (SPEC 6.3): a subscribed member plus his presence."""

    tg_id: int
    gamertag: str | None
    xuid: str
    state: str | None
    title_id: str | None
    title_name: str | None


@dataclass(slots=True)
class ChatMemberStat:
    tg_id: int
    gamertag: str | None
    xuid: str
    count: int
    score: int
    rare: int


@dataclass(slots=True)
class RecentAchievement:
    gamertag: str | None
    name: str
    game: str | None
    gamerscore: int
    rarity_percent: float | None
    platform: str
    unlocked_at: str | None


@dataclass(slots=True)
class TopGame:
    name: str | None
    gamerscore: int | None
    unlocked: int | None
    total: int | None


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


@dataclass(slots=True)
class HltbCacheRow:
    hltb_id: int
    name: str
    release_year: int | None
    main_hours: float | None
    extra_hours: float | None
    completionist_hours: float | None
    platforms: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PlatformLink:
    tg_id: int
    platform: str
    external_id: str
    display_name: str | None
    linked_at: str


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
        """/disconnect_xbox: the link goes, seen_achievements and history stay (SPEC 6.1)."""
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

    async def presence_of(self, xuid: str) -> PresenceRow | None:
        cursor = await self._conn.execute(
            "SELECT xuid, state, title_id, title_name, updated_at FROM presence_state "
            "WHERE xuid = ?",
            (xuid,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return PresenceRow(
            xuid=row["xuid"],
            state=row["state"],
            title_id=row["title_id"],
            title_name=row["title_name"],
            updated_at=row["updated_at"],
        )

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

    async def last_achievement(self, xuid: str) -> AchievementRow | None:
        """The most recent unlock. Undated rows never win."""
        rows = await self.recent_achievements(xuid, limit=1)
        return rows[0] if rows else None

    async def recent_achievements(self, xuid: str, limit: int = 5) -> list[AchievementRow]:
        """The last N unlocks, newest first — for the panel (SPEC 6.2).
        Undated rows never win: an unknown unlock time is not "recent"."""
        cursor = await self._conn.execute(
            "SELECT s.*, t.name AS game FROM seen_achievements s "
            "LEFT JOIN titles t ON t.title_id = s.title_id "
            "WHERE s.xuid = ? AND s.unlocked_at IS NOT NULL "
            "ORDER BY s.unlocked_at DESC LIMIT ?",
            (xuid, limit),
        )
        return [
            AchievementRow(
                title_id=row["title_id"],
                achievement_id=row["achievement_id"],
                name=row["name"],
                description=row["description"],
                icon_url=row["icon_url"],
                unlocked_at=row["unlocked_at"],
                gamerscore=row["gamerscore"],
                rarity_percent=row["rarity_percent"],
                platform=row["platform"],
                title_name=row["game"],
            )
            for row in await cursor.fetchall()
        ]

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

    async def chat_exists(self, chat_id: int) -> bool:
        cursor = await self._conn.execute("SELECT 1 FROM chats WHERE chat_id = ?", (chat_id,))
        return await cursor.fetchone() is not None

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
            "SELECT c.chat_id, c.title, s.min_gamerscore, s.muted_title_ids,"
            "       s.rare_threshold_percent, s.daily_summary_time, s.tz_offset_min "
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
                min_gamerscore=row["min_gamerscore"],
                muted_title_ids=json.loads(row["muted_title_ids"] or "[]"),
                rare_threshold_percent=row["rare_threshold_percent"],
                daily_summary_time=row["daily_summary_time"],
                tz_offset_min=row["tz_offset_min"],
            )
            for row in await cursor.fetchall()
        ]

    async def get_chat_daily_settings(self, chat_id: int) -> ChatDailySettings:
        cursor = await self._conn.execute(
            "SELECT rare_threshold_percent, daily_summary_time, tz_offset_min "
            "FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            # A chat with no chat_settings row at all — should not happen
            # (every chat gets one via upsert_chat), the hardcoded defaults
            # are the same ones a brand new row would carry.
            return ChatDailySettings(10.0, "20:00", 180)
        return ChatDailySettings(
            rare_threshold_percent=row["rare_threshold_percent"],
            daily_summary_time=row["daily_summary_time"],
            tz_offset_min=row["tz_offset_min"],
        )

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

    # ------------------------------------------------------------ aggregates

    async def achievement_counts(self, xuid: str, since: datetime | None) -> tuple[int, int]:
        """How many achievements and how much gamerscore since a moment.

        Counted regardless of `is_backfill` (SPEC 5.9). Timestamps are stored
        as UTC ISO strings of one shape, so a string comparison is a time
        comparison here.
        """
        if since is None:
            cursor = await self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(gamerscore), 0) FROM seen_achievements "
                "WHERE xuid = ?",
                (xuid,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(gamerscore), 0) FROM seen_achievements "
                "WHERE xuid = ? AND unlocked_at >= ?",
                (xuid, _iso(since)),
            )
        row = await cursor.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    async def achievement_counts_by_xuid(
        self, since: datetime | None
    ) -> dict[str, tuple[int, int]]:
        """The same numbers for everyone at once — one query for a whole page."""
        if since is None:
            cursor = await self._conn.execute(
                "SELECT xuid, COUNT(*), COALESCE(SUM(gamerscore), 0) "
                "FROM seen_achievements GROUP BY xuid"
            )
        else:
            cursor = await self._conn.execute(
                "SELECT xuid, COUNT(*), COALESCE(SUM(gamerscore), 0) "
                "FROM seen_achievements WHERE unlocked_at >= ? GROUP BY xuid",
                (_iso(since),),
            )
        return {row[0]: (int(row[1]), int(row[2])) for row in await cursor.fetchall()}

    # ------------------------------------------------------------ chat stats

    async def chat_member_stats(
        self,
        chat_id: int,
        since: datetime,
        rare_threshold: float,
        until: datetime | None = None,
    ) -> list[ChatMemberStat]:
        """Per-person totals for a chat over a period.

        Excluded users drop out here; everyone else appears even with a zero,
        including rows the feed filtered out — the summary is a report, not
        the feed (SPEC 7.3). The date filter lives in the JOIN, not WHERE: a
        WHERE on the right-hand table turns a LEFT JOIN back into an INNER
        JOIN, which is exactly the bug that used to hide zero-scorers.
        """
        date_bound = "AND s.unlocked_at >= ?"
        date_params: list[object] = [_iso(since)]
        if until is not None:
            date_bound += " AND s.unlocked_at < ?"
            date_params.append(_iso(until))

        cursor = await self._conn.execute(
            "SELECT u.tg_id, u.gamertag, u.xuid, COUNT(s.achievement_id) AS cnt,"
            "       COALESCE(SUM(s.gamerscore), 0) AS score,"
            "       SUM(CASE WHEN s.rarity_percent IS NOT NULL AND s.rarity_percent <= ?"
            "                THEN 1 ELSE 0 END) AS rare "
            "FROM subscriptions sub "
            "JOIN users u ON u.tg_id = sub.tg_id "
            "LEFT JOIN seen_achievements s ON s.xuid = u.xuid " + date_bound + " "
            "WHERE sub.chat_id = ? AND u.is_excluded = 0 "
            "GROUP BY u.tg_id ORDER BY cnt DESC, score DESC",
            [rare_threshold, *date_params, chat_id],
        )
        return [
            ChatMemberStat(
                tg_id=row["tg_id"],
                gamertag=row["gamertag"],
                xuid=row["xuid"],
                count=int(row["cnt"]),
                score=int(row["score"]),
                rare=int(row["rare"] or 0),
            )
            for row in await cursor.fetchall()
        ]

    async def chat_member_presence(self, chat_id: int) -> list[ChatPresenceRow]:
        """Every connected, non-excluded member *known to be in this chat*,
        with his last known presence — for /online (SPEC 6.3). "Known to be
        in this chat" is the union of who publishes here (`subscriptions`)
        and who has just been seen writing here (`chat_seen`) — publishing
        and being a member are not the same thing, and /online listing only
        publishers made it look like nobody else was playing. Playing-now
        first, then online, then the rest, so the people actually worth
        pinging float to the top."""
        cursor = await self._conn.execute(
            "SELECT u.tg_id, u.gamertag, u.xuid, p.state, p.title_id, p.title_name "
            "FROM ("
            "  SELECT tg_id FROM subscriptions WHERE chat_id = ? "
            "  UNION "
            "  SELECT tg_id FROM chat_seen WHERE chat_id = ?"
            ") member "
            "JOIN users u ON u.tg_id = member.tg_id "
            "LEFT JOIN presence_state p ON p.xuid = u.xuid "
            "WHERE u.xuid IS NOT NULL AND u.is_excluded = 0 "
            "ORDER BY "
            "  CASE WHEN p.state = 'Online' AND p.title_id IS NOT NULL THEN 0 "
            "       WHEN p.state = 'Online' THEN 1 "
            "       ELSE 2 END, "
            "  u.gamertag",
            (chat_id, chat_id),
        )
        return [
            ChatPresenceRow(
                tg_id=row["tg_id"],
                gamertag=row["gamertag"],
                xuid=row["xuid"],
                state=row["state"],
                title_id=row["title_id"],
                title_name=row["title_name"],
            )
            for row in await cursor.fetchall()
        ]

    async def record_chat_seen(self, chat_id: int, tg_id: int) -> None:
        """A message from a *known* tg_id in this group — feeds /online's
        membership list (SPEC 6.3). The `SELECT ... WHERE EXISTS` guard keeps
        the same "never create a user row just for writing a message" rule
        as `update_username`: someone the bot has no `users` row for yet
        leaves no trace here either."""
        await self._conn.execute(
            "INSERT INTO chat_seen (chat_id, tg_id, last_seen_at) "
            "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM users WHERE tg_id = ?) "
            "ON CONFLICT (chat_id, tg_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (chat_id, tg_id, utcnow_iso(), tg_id),
        )
        await self._conn.commit()

    async def chat_subscriber_names(self, chat_id: int) -> list[str]:
        cursor = await self._conn.execute(
            "SELECT u.gamertag, u.tg_id FROM subscriptions s "
            "JOIN users u ON u.tg_id = s.tg_id "
            "WHERE s.chat_id = ? AND u.is_excluded = 0 "
            "ORDER BY u.gamertag",
            (chat_id,),
        )
        return [row["gamertag"] or f"id{row['tg_id']}" for row in await cursor.fetchall()]

    async def chat_recent(self, chat_id: int, limit: int) -> list[RecentAchievement]:
        cursor = await self._conn.execute(
            "SELECT u.gamertag, s.name, t.name AS game, s.gamerscore, s.rarity_percent,"
            "       s.platform, s.unlocked_at "
            "FROM subscriptions sub "
            "JOIN users u ON u.tg_id = sub.tg_id "
            "JOIN seen_achievements s ON s.xuid = u.xuid "
            "LEFT JOIN titles t ON t.title_id = s.title_id "
            "WHERE sub.chat_id = ? AND u.is_excluded = 0 AND s.unlocked_at IS NOT NULL "
            "ORDER BY s.unlocked_at DESC LIMIT ?",
            (chat_id, limit),
        )
        return [
            RecentAchievement(
                gamertag=row["gamertag"],
                name=row["name"],
                game=row["game"],
                gamerscore=int(row["gamerscore"] or 0),
                rarity_percent=row["rarity_percent"],
                platform=row["platform"],
                unlocked_at=row["unlocked_at"],
            )
            for row in await cursor.fetchall()
        ]

    async def top_games(self, xuid: str, limit: int = 5) -> list[TopGame]:
        cursor = await self._conn.execute(
            "SELECT t.name, h.current_gamerscore, h.achievements_unlocked, h.achievements_total "
            "FROM title_history h LEFT JOIN titles t ON t.title_id = h.title_id "
            "WHERE h.xuid = ? AND h.current_gamerscore > 0 "
            "ORDER BY h.current_gamerscore DESC LIMIT ?",
            (xuid, limit),
        )
        return [
            TopGame(
                name=row["name"],
                gamerscore=row["current_gamerscore"],
                unlocked=row["achievements_unlocked"],
                total=row["achievements_total"],
            )
            for row in await cursor.fetchall()
        ]

    async def recent_games(self, xuid: str, since: datetime, limit: int = 15) -> list[TopGame]:
        """Games actually played recently, not the biggest lifetime scores —
        a person's five favourite old games would otherwise crowd out
        whatever they are playing this month, every time."""
        cursor = await self._conn.execute(
            "SELECT t.name, COALESCE(SUM(s.gamerscore), 0) AS score, COUNT(*) AS unlocked "
            "FROM seen_achievements s LEFT JOIN titles t ON t.title_id = s.title_id "
            "WHERE s.xuid = ? AND s.unlocked_at >= ? "
            "GROUP BY s.title_id ORDER BY score DESC LIMIT ?",
            (xuid, _iso(since), limit),
        )
        return [
            TopGame(name=row["name"], gamerscore=row["score"], unlocked=row["unlocked"], total=None)
            for row in await cursor.fetchall()
        ]

    async def chat_recent_games(self, chat_id: int, limit: int = 10) -> list[str]:
        """Distinct game names the chat's known members have actually played
        recently, most-recent first — quick-pick shortcuts for /hltb so the
        common case ("what does everyone here keep talking about") needs no
        typing at all (SPEC 6.6). Same membership as /online and /who: the
        union of who publishes here and who's just been seen writing here,
        not only publishers."""
        cursor = await self._conn.execute(
            "SELECT t.name AS name, MAX(th.last_played_at) AS last_played "
            "FROM title_history th "
            "JOIN titles t ON t.title_id = th.title_id "
            "WHERE th.xuid IN ("
            "  SELECT u.xuid FROM users u "
            "  WHERE u.xuid IS NOT NULL AND u.tg_id IN ("
            "    SELECT tg_id FROM subscriptions WHERE chat_id = ? "
            "    UNION "
            "    SELECT tg_id FROM chat_seen WHERE chat_id = ?"
            "  )"
            ") AND th.last_played_at IS NOT NULL "
            "GROUP BY t.name "
            "ORDER BY last_played DESC LIMIT ?",
            (chat_id, chat_id, limit),
        )
        return [row["name"] for row in await cursor.fetchall() if row["name"]]

    async def find_user_by_username(self, username: str) -> User | None:
        cursor = await self._conn.execute(
            "SELECT * FROM users WHERE lower(username) = lower(?)", (username.lstrip("@"),)
        )
        row = await cursor.fetchone()
        return _as_user(row) if row else None

    async def daily_report_sent(self, chat_id: int, report_date: str) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM daily_reports WHERE chat_id = ? AND report_date = ?",
            (chat_id, report_date),
        )
        return await cursor.fetchone() is not None

    async def mark_daily_report_sent(self, chat_id: int, report_date: str) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO daily_reports (chat_id, report_date, sent_at) VALUES (?, ?, ?)",
            (chat_id, report_date, utcnow_iso()),
        )
        await self._conn.commit()

    async def log_bot_message(self, chat_id: int, message_id: int) -> None:
        """Called from the request middleware (bot/services/message_log.py)
        for every message the bot sends to a group — the only record that
        lets the admin panel's "стереть сообщения бота" find anything to
        delete (SPEC 6.4)."""
        await self._conn.execute(
            "INSERT OR IGNORE INTO bot_messages (chat_id, message_id, sent_at) VALUES (?, ?, ?)",
            (chat_id, message_id, utcnow_iso()),
        )
        await self._conn.commit()

    async def bot_messages_since(self, chat_id: int, since: datetime) -> list[int]:
        cursor = await self._conn.execute(
            "SELECT message_id FROM bot_messages WHERE chat_id = ? AND sent_at >= ?",
            (chat_id, _iso(since)),
        )
        return [row[0] for row in await cursor.fetchall()]

    async def forget_bot_messages(self, chat_id: int, message_ids: Sequence[int]) -> None:
        """Drops the log rows after an actual delete attempt — called
        regardless of whether Telegram could delete every one of them (some
        may already be gone), since there is nothing more to do about those
        either way."""
        await self._conn.executemany(
            "DELETE FROM bot_messages WHERE chat_id = ? AND message_id = ?",
            [(chat_id, message_id) for message_id in message_ids],
        )
        await self._conn.commit()

    # ----------------------------------------------------------------- admin

    async def admin_users(self) -> list[AdminUserRow]:
        cursor = await self._conn.execute(
            "SELECT u.tg_id, u.gamertag, u.username, u.xuid, u.gamerscore, u.is_excluded,"
            "       u.last_online_at, t.status, t.last_refresh_at "
            "FROM users u LEFT JOIN tokens t ON t.tg_id = u.tg_id "
            "WHERE u.xuid IS NOT NULL "
            "ORDER BY u.is_excluded, u.last_online_at DESC"
        )
        return [
            AdminUserRow(
                tg_id=row["tg_id"],
                gamertag=row["gamertag"],
                username=row["username"],
                xuid=row["xuid"],
                gamerscore=row["gamerscore"],
                is_excluded=bool(row["is_excluded"]),
                last_online_at=row["last_online_at"],
                token_status=row["status"],
                last_refresh_at=row["last_refresh_at"],
            )
            for row in await cursor.fetchall()
        ]

    async def set_excluded(self, tg_id: int, excluded: bool, by: int | None) -> None:
        """Exclusion is never silent: the person sees it in his panel (SPEC 6.4)."""
        await self._conn.execute(
            "UPDATE users SET is_excluded = ?, excluded_by = ?, excluded_at = ?, updated_at = ? "
            "WHERE tg_id = ?",
            (
                1 if excluded else 0,
                by if excluded else None,
                utcnow_iso() if excluded else None,
                utcnow_iso(),
                tg_id,
            ),
        )
        await self._conn.commit()

    async def admin_chats(self) -> list[ChatTarget]:
        cursor = await self._conn.execute(
            "SELECT c.chat_id, c.title, c.is_active, s.min_gamerscore,"
            "       s.daily_summary, s.muted_title_ids, s.rare_threshold_percent,"
            "       s.daily_summary_time, s.tz_offset_min,"
            "       (SELECT COUNT(*) FROM subscriptions WHERE chat_id = c.chat_id) AS subs "
            "FROM chats c JOIN chat_settings s ON s.chat_id = c.chat_id "
            "ORDER BY c.is_active DESC, c.title"
        )
        return [
            ChatTarget(
                chat_id=row["chat_id"],
                title=row["title"],
                min_gamerscore=row["min_gamerscore"],
                muted_title_ids=json.loads(row["muted_title_ids"] or "[]"),
                rare_threshold_percent=row["rare_threshold_percent"],
                daily_summary_time=row["daily_summary_time"],
                tz_offset_min=row["tz_offset_min"],
                is_active=bool(row["is_active"]),
                daily_summary=bool(row["daily_summary"]),
                subscribers=int(row["subs"]),
            )
            for row in await cursor.fetchall()
        ]

    async def update_chat_settings(self, chat_id: int, **fields: Any) -> None:
        allowed = {
            "min_gamerscore",
            "daily_summary",
            "rare_threshold_percent",
            "daily_summary_time",
            "tz_offset_min",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown chat_settings fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._conn.execute(
            f"UPDATE chat_settings SET {assignments} WHERE chat_id = ?",
            (*fields.values(), chat_id),
        )
        await self._conn.commit()

    async def set_chat_active(self, chat_id: int, active: bool) -> None:
        await self._conn.execute(
            "UPDATE chats SET is_active = ? WHERE chat_id = ?", (1 if active else 0, chat_id)
        )
        await self._conn.commit()

    async def upsert_title(self, title_id: str, name: str, platform: str | None) -> None:
        await self._conn.execute(
            "INSERT INTO titles (title_id, name, platform, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(title_id) DO UPDATE SET name = excluded.name,"
            " platform = excluded.platform, updated_at = excluded.updated_at",
            (title_id, name, platform, utcnow_iso()),
        )
        await self._conn.commit()

    async def title_name(self, title_id: str) -> str | None:
        cursor = await self._conn.execute("SELECT name FROM titles WHERE title_id = ?", (title_id,))
        row = await cursor.fetchone()
        return row["name"] if row else None

    async def hltb_all_ids(self) -> list[int]:
        """For the one-off platforms backfill (scripts/backfill_hltb_platforms.py)
        — every id already cached, so it can be re-resolved with the field
        that didn't exist when it was first cached."""
        cursor = await self._conn.execute("SELECT hltb_id FROM hltb_cache")
        return [row[0] for row in await cursor.fetchall()]

    async def hltb_get_cached(self, hltb_id: int) -> HltbCacheRow | None:
        cursor = await self._conn.execute(
            "SELECT hltb_id, name, release_year, main_hours, extra_hours,"
            " completionist_hours, platforms "
            "FROM hltb_cache WHERE hltb_id = ?",
            (hltb_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return HltbCacheRow(
            hltb_id=row["hltb_id"],
            name=row["name"],
            release_year=row["release_year"],
            main_hours=row["main_hours"],
            extra_hours=row["extra_hours"],
            completionist_hours=row["completionist_hours"],
            platforms=json.loads(row["platforms"] or "[]"),
        )

    async def hltb_cache_result(self, entry: HltbCacheRow) -> None:
        """Cached forever (SPEC 6.6) — only called once someone actually
        picks a search result, never for the rest of the candidate list."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO hltb_cache "
            "(hltb_id, name, release_year, main_hours, extra_hours, completionist_hours,"
            " platforms, cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.hltb_id,
                entry.name,
                entry.release_year,
                entry.main_hours,
                entry.extra_hours,
                entry.completionist_hours,
                json.dumps(entry.platforms),
                utcnow_iso(),
            ),
        )
        await self._conn.commit()

    async def link_platform_account(
        self, tg_id: int, platform: str, external_id: str, display_name: str | None
    ) -> None:
        """One row per (person, platform) — a second /connect_steam replaces
        the link, same as reconnecting Xbox replaces the old identity."""
        await self._conn.execute(
            "INSERT INTO platform_links (tg_id, platform, external_id, display_name, linked_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_id, platform) DO UPDATE SET "
            "  external_id = excluded.external_id,"
            "  display_name = excluded.display_name,"
            "  linked_at = excluded.linked_at",
            (tg_id, platform, external_id, display_name, utcnow_iso()),
        )
        await self._conn.commit()

    async def get_platform_link(self, tg_id: int, platform: str) -> PlatformLink | None:
        cursor = await self._conn.execute(
            "SELECT tg_id, platform, external_id, display_name, linked_at "
            "FROM platform_links WHERE tg_id = ? AND platform = ?",
            (tg_id, platform),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return PlatformLink(
            tg_id=row["tg_id"],
            platform=row["platform"],
            external_id=row["external_id"],
            display_name=row["display_name"],
            linked_at=row["linked_at"],
        )

    async def platform_links_of(self, tg_id: int) -> list[PlatformLink]:
        cursor = await self._conn.execute(
            "SELECT tg_id, platform, external_id, display_name, linked_at "
            "FROM platform_links WHERE tg_id = ?",
            (tg_id,),
        )
        return [
            PlatformLink(
                tg_id=row["tg_id"],
                platform=row["platform"],
                external_id=row["external_id"],
                display_name=row["display_name"],
                linked_at=row["linked_at"],
            )
            for row in await cursor.fetchall()
        ]

    async def unlink_platform_account(self, tg_id: int, platform: str) -> None:
        await self._conn.execute(
            "DELETE FROM platform_links WHERE tg_id = ? AND platform = ?", (tg_id, platform)
        )
        await self._conn.commit()


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


def _iso(moment: datetime) -> str:
    """Stored timestamps are UTC ISO strings truncated to seconds."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")
