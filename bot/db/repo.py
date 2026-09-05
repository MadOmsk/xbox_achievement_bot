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
class SteamPollTarget:
    """Steam's counterpart of `PollTarget` (SPEC 9, M-Steam-2c) — same
    shape, Steam's own field names (`persona_state`/`gameid`/`game_name`
    instead of `state`/`title_id`/`title_name`)."""

    tg_id: int
    steam_id: str
    persona_state: int | None
    gameid: str | None
    game_name: str | None
    changed_at: str | None
    last_ach_poll_at: str | None
    updated_at: str | None
    # Sticky through brief presence gaps — see steam_presence_state's own
    # comment (schema.sql) and poller/steam_presence.py's grace period.
    last_active_gameid: str | None = None
    last_active_game_name: str | None = None
    last_active_at: str | None = None


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
    is_secret: bool = False


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
    # The person's own choice for *this* chat (SPEC 9, M-Steam-2e's
    # follow-up — moved off user_settings, one value for every chat, onto
    # subscriptions, one value per chat). Defaults to 'all' only for call
    # sites (admin_chats) that have no one specific subscriber in mind.
    rarity_mode: str = "all"
    # N+ achievements in one game at once collapse into a summary message
    # instead of separate ones — per (person, chat), same follow-up as
    # rarity_mode above and for the same reason (2026-09-05). Default only
    # applies to call sites (admin_chats) with no one specific subscriber.
    digest_threshold: int = 3
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
class UserChatRow:
    """One chat a person has ever touched — subscribed at some point, or
    just seen writing there (SPEC 6.2's "Мои чаты") — with whether they are
    publishing there right now."""

    chat_id: int
    title: str | None
    is_subscribed: bool
    rarity_mode: str | None  # only meaningful while subscribed; None otherwise
    digest_threshold: int | None  # same — per subscription, None while not subscribed


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
    """One row of /online (SPEC 6.3): a subscribed member plus his presence.

    `state`/`title_id`/`title_name` are already normalized to Xbox's own
    vocabulary regardless of which platform they actually came from (SPEC 9,
    M-Steam-2e) — the query picks whichever platform's presence updated more
    recently and translates Steam's own persona_state/gameid into the same
    shape, so the "playing/online/offline" wording never needs to know
    Steam presence exists. `platform` is exposed separately, only for the
    icon colour next to the name — not mixed into `state`.
    """

    tg_id: int
    gamertag: str | None
    xuid: str | None  # can be unset for a Steam-only person
    state: str | None
    title_id: str | None
    title_name: str | None
    platform: str  # whichever platform state/title_id/title_name came from


@dataclass(slots=True)
class ChatMemberStat:
    tg_id: int
    gamertag: str | None
    xuid: str | None  # can be unset for a Steam-only person (SPEC 9, M-Steam-2e)
    count: int
    score: int
    rare: int
    # Behind `count`'s single combined total (2026-09-05 follow-up) — a
    # parenthetical next to it, not a second sort key or a second row.
    xbox_count: int = 0
    steam_count: int = 0


@dataclass(slots=True)
class RecentAchievement:
    gamertag: str | None
    name: str
    game: str | None
    gamerscore: int
    rarity_percent: float | None
    platform: str
    unlocked_at: str | None
    is_secret: bool = False


@dataclass(slots=True)
class TopGame:
    name: str | None
    gamerscore: int | None
    unlocked: int | None
    platform: str | None = None


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
    game_url: str | None = None
    image_url: str | None = None
    genre: str | None = None


@dataclass(slots=True)
class PlatformLink:
    tg_id: int
    platform: str
    external_id: str
    display_name: str | None
    linked_at: str


@dataclass(slots=True)
class SteamSchemaAchievement:
    """One achievement's game-level (not per-person) data — the game's
    achievement list itself, cached forever (SPEC 9, M-Steam-2b)."""

    apiname: str
    icon: str | None
    hidden: bool


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
        allowed = {"tz_offset_min"}
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

    # ------------------------------------------------------- Steam polling

    async def steam_pollable_users(self) -> list[SteamPollTarget]:
        """Who the Steam poller may look at (SPEC 9, M-Steam-2c) — no `tokens`
        JOIN here unlike `pollable_users()`: Steam has no per-user OAuth at
        all, one shared API key for the whole bot (M-Steam-1)."""
        cursor = await self._conn.execute(
            "SELECT u.tg_id, pl.external_id AS steam_id, p.persona_state, p.gameid,"
            "       p.game_name, p.changed_at, p.last_ach_poll_at, p.updated_at,"
            "       p.last_active_gameid, p.last_active_game_name, p.last_active_at "
            "FROM platform_links pl "
            "JOIN users u ON u.tg_id = pl.tg_id "
            "LEFT JOIN steam_presence_state p ON p.steam_id = pl.external_id "
            "WHERE pl.platform = 'steam' AND u.is_excluded = 0"
        )
        return [
            SteamPollTarget(
                tg_id=row["tg_id"],
                steam_id=row["steam_id"],
                persona_state=row["persona_state"],
                gameid=row["gameid"],
                game_name=row["game_name"],
                changed_at=row["changed_at"],
                last_ach_poll_at=row["last_ach_poll_at"],
                updated_at=row["updated_at"],
                last_active_gameid=row["last_active_gameid"],
                last_active_game_name=row["last_active_game_name"],
                last_active_at=row["last_active_at"],
            )
            for row in await cursor.fetchall()
        ]

    async def save_steam_presence_state(
        self,
        steam_id: str,
        persona_state: int,
        gameid: str | None,
        game_name: str | None,
        *,
        changed: bool,
    ) -> None:
        now = utcnow_iso()
        # last_active_* only moves forward when this tick actually has a
        # gameid — a tick that finds none (presence gap or a real quit)
        # leaves it exactly where it was, which is the whole point: it's
        # what the grace period (poller/steam_presence.py) reads to decide
        # whether "no gameid right now" still means "keep polling the last
        # game anyway". On first-ever insert there's no prior tick to fall
        # back to, so it just starts out matching this one (NULL together
        # with gameid if this row's very first sighting has no game either).
        last_active_at = now if gameid is not None else None
        await self._conn.execute(
            "INSERT INTO steam_presence_state "
            "(steam_id, persona_state, gameid, game_name, changed_at, updated_at,"
            " last_active_gameid, last_active_game_name, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(steam_id) DO UPDATE SET "
            "  persona_state = excluded.persona_state, gameid = excluded.gameid,"
            "  game_name = excluded.game_name, updated_at = excluded.updated_at,"
            "  changed_at = CASE WHEN ? THEN excluded.changed_at "
            "                 ELSE steam_presence_state.changed_at END,"
            "  last_active_gameid = CASE WHEN excluded.gameid IS NOT NULL"
            "    THEN excluded.gameid ELSE steam_presence_state.last_active_gameid END,"
            "  last_active_game_name = CASE WHEN excluded.gameid IS NOT NULL"
            "    THEN excluded.game_name ELSE steam_presence_state.last_active_game_name END,"
            "  last_active_at = CASE WHEN excluded.gameid IS NOT NULL"
            "    THEN excluded.updated_at ELSE steam_presence_state.last_active_at END",
            (
                steam_id,
                persona_state,
                gameid,
                game_name,
                now,
                now,
                gameid,
                game_name,
                last_active_at,
                1 if changed else 0,
            ),
        )
        await self._conn.commit()

    async def mark_steam_achievements_polled(self, steam_id: str) -> None:
        await self._conn.execute(
            "UPDATE steam_presence_state SET last_ach_poll_at = ? WHERE steam_id = ?",
            (utcnow_iso(), steam_id),
        )
        await self._conn.commit()

    async def delete_steam_presence_state(self, steam_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM steam_presence_state WHERE steam_id = ?", (steam_id,)
        )
        await self._conn.commit()

    # -------------------------------------------------------- achievements

    async def insert_new_achievements(
        self, xuid: str, achievements: Sequence[AchievementRow], *, is_backfill: bool
    ) -> list[AchievementRow]:
        """Insert what we have not seen and report back only the new rows.

        The primary key (tg_id, platform, title_id, achievement_id) is the
        deduplication: INSERT OR IGNORE tells us which rows were actually
        new. tg_id, not xuid, is what identifies whose row this is (SPEC
        9, M-Steam-2) — resolved here from xuid so every existing (Xbox-
        only) caller keeps working unchanged; a future Steam call site
        would resolve its own tg_id from platform_links instead and this
        method would need a platform-aware variant.
        """
        if not achievements:
            return []
        owner = await self.get_user_by_xuid(xuid)
        if owner is None:  # defensive — an xuid always comes from a connected user
            log.warning(
                "insert_new_achievements: no user for xuid=%s, dropped %d rows",
                xuid,
                len(achievements),
            )
            return []
        tg_id = owner.tg_id

        new_rows: list[AchievementRow] = []
        now = utcnow_iso()
        for item in achievements:
            cursor = await self._conn.execute(
                "INSERT OR IGNORE INTO seen_achievements "
                "(tg_id, xuid, title_id, achievement_id, name, description, icon_url, unlocked_at,"
                " gamerscore, rarity_percent, platform, is_backfill, is_secret, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tg_id,
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
                    1 if item.is_secret else 0,
                    now,
                ),
            )
            if cursor.rowcount:
                new_rows.append(item)
        await self._conn.commit()
        return new_rows

    async def insert_new_achievements_steam(
        self,
        tg_id: int,
        steam_id: str,
        achievements: Sequence[AchievementRow],
        *,
        is_backfill: bool,
    ) -> list[AchievementRow]:
        """Steam's counterpart of `insert_new_achievements` (SPEC 9, M-Steam-
        2c/2d) — no xuid-lookup stopgap needed here: the Steam poller/backfill
        always already has `tg_id` on hand, straight from `platform_links`,
        unlike the Xbox path which only ever has an xuid to start from. The
        `xuid` column in `seen_achievements` is the same generic per-platform
        external_id it's always been (SPEC 9, M-Steam-2a) — holds the
        SteamID64 here, not an Xbox xuid.
        """
        if not achievements:
            return []

        # Xbox's own fetcher caches a title's name via ensure_title_name() on
        # every poll (poller/fetcher.py) — Steam never had an equivalent, so
        # `titles` stayed empty for every appid and /recent's LEFT JOIN onto
        # it fell back to "без названия" for every Steam row. One upsert per
        # unique game in this batch, not per achievement.
        cached_titles: dict[str, str] = {}
        for item in achievements:
            if item.title_name and item.title_id not in cached_titles:
                cached_titles[item.title_id] = item.title_name
        for title_id, name in cached_titles.items():
            await self.upsert_title(title_id, name, "steam")

        new_rows: list[AchievementRow] = []
        now = utcnow_iso()
        for item in achievements:
            cursor = await self._conn.execute(
                "INSERT OR IGNORE INTO seen_achievements "
                "(tg_id, xuid, title_id, achievement_id, name, description, icon_url, unlocked_at,"
                " gamerscore, rarity_percent, platform, is_backfill, is_secret, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tg_id,
                    steam_id,
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
                    1 if item.is_secret else 0,
                    now,
                ),
            )
            if cursor.rowcount:
                new_rows.append(item)
        await self._conn.commit()
        return new_rows

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
                is_secret=bool(row["is_secret"]),
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
        # rarity_mode is explicit here, not left to the column's own
        # DEFAULT 'all' — an admin-configurable starting point
        # (app_settings['default_rarity_mode'], handlers/admin.py) now
        # decides it instead of a value baked into the schema. The column
        # default stays 'all' regardless, as a safety net for any insert
        # that (today or in the future) doesn't go through this method.
        default_rarity_mode = await self.get_app_setting("default_rarity_mode", "all")
        await self._conn.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, tg_id, created_at, rarity_mode) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, tg_id, utcnow_iso(), default_rarity_mode),
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

    async def user_chats(self, tg_id: int) -> list[UserChatRow]:
        """Every chat this person has ever touched (SPEC 6.2's "Мои чаты") —
        subscribed at some point, or just seen writing there, same membership
        `/online` uses (SPEC 6.3). A chat the bot got kicked from is left out:
        nothing to manage there any more. `rarity_mode`/`digest_threshold`
        come along too (SPEC 9, M-Steam-2e's follow-up, and 2026-09-05's for
        digest_threshold — both live per subscription now), NULL when not
        currently subscribed."""
        cursor = await self._conn.execute(
            "SELECT c.chat_id, c.title, s.rarity_mode, s.digest_threshold "
            "FROM chats c "
            "LEFT JOIN subscriptions s ON s.chat_id = c.chat_id AND s.tg_id = ? "
            "WHERE c.is_active = 1 AND c.chat_id IN ("
            "  SELECT chat_id FROM subscriptions WHERE tg_id = ?"
            "  UNION "
            "  SELECT chat_id FROM chat_seen WHERE tg_id = ?"
            ") ORDER BY c.title",
            (tg_id, tg_id, tg_id),
        )
        return [
            UserChatRow(
                chat_id=row["chat_id"],
                title=row["title"],
                is_subscribed=row["rarity_mode"] is not None,
                rarity_mode=row["rarity_mode"],
                digest_threshold=row["digest_threshold"],
            )
            for row in await cursor.fetchall()
        ]

    async def update_subscription_rarity_mode(
        self, chat_id: int, tg_id: int, rarity_mode: str
    ) -> None:
        """The person's own rarity choice for one specific chat (SPEC 9,
        M-Steam-2e's follow-up — panel.py's "Мои чаты" card, not the main
        panel screen any more)."""
        await self._conn.execute(
            "UPDATE subscriptions SET rarity_mode = ? WHERE chat_id = ? AND tg_id = ?",
            (rarity_mode, chat_id, tg_id),
        )
        await self._conn.commit()

    async def update_subscription_digest_threshold(
        self, chat_id: int, tg_id: int, digest_threshold: int
    ) -> None:
        """The person's own digest threshold for one specific chat
        (Follow-up, 2026-09-05 — panel.py's "Мои чаты" card, not the main
        panel screen any more, same move as rarity_mode above)."""
        await self._conn.execute(
            "UPDATE subscriptions SET digest_threshold = ? WHERE chat_id = ? AND tg_id = ?",
            (digest_threshold, chat_id, tg_id),
        )
        await self._conn.commit()

    async def forget_chat_membership(self, chat_id: int, tg_id: int) -> None:
        """ "Delete" a chat from a person's own list (SPEC 6.2) — resets him to
        as if he had never subscribed or been seen there. Not a ban: writing
        in the chat again, or subscribing again, brings it right back
        (`record_chat_seen`/`subscribe`) — there is no third state that
        blocks that."""
        await self._conn.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND tg_id = ?", (chat_id, tg_id)
        )
        await self._conn.execute(
            "DELETE FROM chat_seen WHERE chat_id = ? AND tg_id = ?", (chat_id, tg_id)
        )
        await self._conn.commit()

    async def deactivate_chat(self, chat_id: int) -> None:
        """Telegram answered 403 — the bot was kicked out (SPEC 5.5)."""
        await self._conn.execute("UPDATE chats SET is_active = 0 WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

    async def publication_targets(self, tg_id: int) -> list[ChatTarget]:
        cursor = await self._conn.execute(
            "SELECT c.chat_id, c.title, s.min_gamerscore, s.muted_title_ids,"
            "       s.rare_threshold_percent, s.daily_summary_time, s.tz_offset_min,"
            "       sub.rarity_mode, sub.digest_threshold "
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
                rarity_mode=row["rarity_mode"],
                digest_threshold=row["digest_threshold"],
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

    async def achievement_counts_for_person(
        self, tg_id: int, since: datetime | None
    ) -> tuple[int, int]:
        """Same as `achievement_counts`, but summed across every platform a
        person has connected (SPEC 9, M-Steam-2e) — `/stats`/`/summary`'s
        counters, not `achievement_counts`' own caller (`scripts/reconcile_
        achievements.py`, which deliberately stays per-Xbox-account: it
        checks one xuid's stored gamerscore against what Xbox itself
        reports for that xuid, a comparison that has no Steam side to sum
        in). `gamerscore` sums correctly here with no special-casing: a
        Steam row's gamerscore is always 0 (services/steam/achievements.py),
        so it never contributes to the sum, by construction, not by a check
        here."""
        if since is None:
            cursor = await self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(gamerscore), 0) FROM seen_achievements "
                "WHERE tg_id = ?",
                (tg_id,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(gamerscore), 0) FROM seen_achievements "
                "WHERE tg_id = ? AND unlocked_at >= ?",
                (tg_id, _iso(since)),
            )
        row = await cursor.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    async def achievement_platform_breakdown(
        self, tg_id: int, since: datetime | None
    ) -> tuple[int, int]:
        """The (xbox, steam) counts behind `achievement_counts_for_person`'s
        single combined total (2026-09-05 follow-up, reversal of "one number
        only" — SPEC 9 M-Steam-2e originally dropped a per-platform split on
        purpose; the parenthetical here doesn't touch that decision, the
        combined number still leads and still sorts). x360 counts as Xbox —
        there's no separate UI concept of "Xbox 360" anywhere outside the
        achievement message itself and the games table's own icon."""
        query = (
            "SELECT SUM(CASE WHEN platform IN ('modern', 'x360') THEN 1 ELSE 0 END),"
            "       SUM(CASE WHEN platform = 'steam' THEN 1 ELSE 0 END) "
            "FROM seen_achievements WHERE tg_id = ?"
        )
        params: list[object] = [tg_id]
        if since is not None:
            query += " AND unlocked_at >= ?"
            params.append(_iso(since))
        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()
        return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)

    async def platform_achievement_count(self, tg_id: int, platform: str) -> int:
        """Lifetime count for one platform (SPEC 9, M-Steam-2e's /stats line
        next to each connected platform) — deliberately not offered for
        Xbox (`achievement_counts` never exposes a since=None total either,
        SPEC 5.4): a lifetime Steam count has no cap to worry about
        (backfill walks the whole owned-games library via GetOwnedGames),
        so it doesn't carry the same "could quietly undercount" risk."""
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM seen_achievements WHERE tg_id = ? AND platform = ?",
            (tg_id, platform),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

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
            "                THEN 1 ELSE 0 END) AS rare,"
            "       SUM(CASE WHEN s.platform IN ('modern', 'x360') THEN 1 ELSE 0 END)"
            "           AS xbox_count,"
            "       SUM(CASE WHEN s.platform = 'steam' THEN 1 ELSE 0 END) AS steam_count "
            "FROM subscriptions sub "
            "JOIN users u ON u.tg_id = sub.tg_id "
            # tg_id, not xuid (SPEC 9, M-Steam-2e) — sums every platform's
            # achievements for this person into one count, since
            # seen_achievements.tg_id is on every row regardless of platform
            # (2a). gamerscore stays Xbox-only automatically: a Steam row's
            # gamerscore is always 0 (services/steam/achievements.py).
            "LEFT JOIN seen_achievements s ON s.tg_id = u.tg_id " + date_bound + " "
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
                xbox_count=int(row["xbox_count"] or 0),
                steam_count=int(row["steam_count"] or 0),
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
        pinging float to the top.

        Merges Xbox and Steam presence (SPEC 9, M-Steam-2e) by activity
        level first, freshness only as a tiebreaker — **not** "whichever
        updated more recently wins" outright: found live, that version
        showed someone as idle-on-Xbox instead of actively-playing-on-
        Steam simply because Xbox happened to get polled a moment later,
        which every poll does regardless of whether anything changed
        (`save_presence_state`/`save_steam_presence_state` bump
        `updated_at` on every tick). "Playing" always beats "online",
        which always beats "offline/no data", on whichever platform it's
        true on; `updated_at` only decides between two platforms tied at
        the *same* level (both playing, or both merely online) — matching
        the "играет > онлайн > офлайн" rule this was designed to have from
        the start.

        Normalized into the same `state`/`title_id`/`title_name` shape
        Xbox always used, so the "playing/online/offline" wording
        (`_presence_text`, `handlers/chat.py`) never needs to know Steam
        presence exists. `platform` is reported alongside it too, only for
        the icon colour next to the name (`_presence_icon`) — falls back
        to whichever platform the person actually has connected when
        neither has any presence data at all. A person known only through
        Steam now appears here too — used to require `u.xuid IS NOT NULL`,
        which silently dropped Steam-only members entirely.
        """
        cursor = await self._conn.execute(
            "WITH member AS ("
            "  SELECT tg_id FROM subscriptions WHERE chat_id = ? "
            "  UNION "
            "  SELECT tg_id FROM chat_seen WHERE chat_id = ?"
            "), presence AS ("
            "  SELECT u.tg_id, u.gamertag, u.xuid,"
            "         xp.state AS xbox_state, xp.title_id AS xbox_title_id,"
            "         xp.title_name AS xbox_title_name, xp.updated_at AS xbox_updated_at,"
            "         sp.persona_state AS steam_persona_state, sp.gameid AS steam_gameid,"
            "         sp.game_name AS steam_game_name, sp.updated_at AS steam_updated_at,"
            "         pl.external_id AS steam_external_id,"
            "         CASE WHEN xp.state = 'Online' AND xp.title_id IS NOT NULL THEN 2"
            "              WHEN xp.state = 'Online' THEN 1"
            "              ELSE 0 END AS xbox_level,"
            "         CASE WHEN sp.persona_state IS NOT NULL AND sp.persona_state != 0"
            "                   AND sp.gameid IS NOT NULL THEN 2"
            "              WHEN sp.persona_state IS NOT NULL AND sp.persona_state != 0 THEN 1"
            "              ELSE 0 END AS steam_level"
            "  FROM member"
            "  JOIN users u ON u.tg_id = member.tg_id"
            "  LEFT JOIN presence_state xp ON xp.xuid = u.xuid"
            "  LEFT JOIN platform_links pl ON pl.tg_id = u.tg_id AND pl.platform = 'steam'"
            "  LEFT JOIN steam_presence_state sp ON sp.steam_id = pl.external_id"
            "  WHERE (u.xuid IS NOT NULL OR pl.external_id IS NOT NULL) AND u.is_excluded = 0"
            "), decided AS ("
            "  SELECT *, CASE"
            "    WHEN steam_level > xbox_level THEN 1"
            "    WHEN steam_level < xbox_level THEN 0"
            "    WHEN steam_level > 0 THEN"  # tied, both actually active — freshness breaks it
            "      CASE WHEN steam_updated_at IS NOT NULL"
            "                AND (xbox_updated_at IS NULL OR steam_updated_at > xbox_updated_at)"
            "           THEN 1 ELSE 0 END"
            "    ELSE"  # tied at zero — nobody's doing anything, fall back to what's connected
            "      CASE WHEN xuid IS NULL THEN 1 ELSE 0 END"
            "    END AS steam_wins"
            "  FROM presence"
            ") "
            "SELECT tg_id, gamertag, xuid,"
            "       CASE WHEN steam_wins THEN"
            "              CASE WHEN steam_persona_state != 0 THEN 'Online' ELSE 'Offline' END"
            "            ELSE xbox_state END AS state,"
            "       CASE WHEN steam_wins THEN steam_gameid ELSE xbox_title_id END"
            "         AS title_id,"
            "       CASE WHEN steam_wins THEN steam_game_name ELSE xbox_title_name END"
            "         AS title_name,"
            "       CASE WHEN steam_wins THEN 'steam' ELSE 'modern' END AS platform "
            "FROM decided "
            "ORDER BY "
            "  CASE WHEN state = 'Online' AND title_id IS NOT NULL THEN 0 "
            "       WHEN state = 'Online' THEN 1 "
            "       ELSE 2 END, "
            "  gamertag",
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
                platform=row["platform"],
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
            "       s.platform, s.unlocked_at, s.is_secret "
            "FROM subscriptions sub "
            "JOIN users u ON u.tg_id = sub.tg_id "
            # tg_id, not xuid (SPEC 9, M-Steam-2a): xuid is Xbox-only on
            # `users`, always NULL for a Steam-only person and never the
            # SteamID64 `seen_achievements.xuid` holds for a Steam row even
            # for someone with both platforms — this join saw Xbox rows only.
            "JOIN seen_achievements s ON s.tg_id = u.tg_id "
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
                is_secret=bool(row["is_secret"]),
            )
            for row in await cursor.fetchall()
        ]

    async def recent_games(
        self, external_id: str, since: datetime, limit: int = 15
    ) -> list[TopGame]:
        """Games actually played recently, not the biggest lifetime scores —
        a person's five favourite old games would otherwise crowd out
        whatever they are playing this month, every time.

        `external_id` despite the historical name isn't Xbox-specific:
        `seen_achievements.xuid` is the generic per-platform external id
        (SPEC 9, M-Steam-2a) — a SteamID64 works here exactly as well as an
        xuid, already scoped to that one account's own rows.

        `limit == 0` means "no cap" (admin-configurable, SPEC 6.4) — passed
        to SQLite as -1, its own documented spelling of "unbounded LIMIT",
        rather than branching the query string for one case.
        """
        cursor = await self._conn.execute(
            "SELECT t.name, COALESCE(SUM(s.gamerscore), 0) AS score, COUNT(*) AS unlocked,"
            " MAX(s.platform) AS platform "
            "FROM seen_achievements s LEFT JOIN titles t ON t.title_id = s.title_id "
            "WHERE s.xuid = ? AND s.unlocked_at >= ? "
            # Score ties on every Steam game (no gamerscore there at all) —
            # unlocked count as the tiebreaker instead of SQLite's undefined
            # order among equal scores.
            "GROUP BY s.title_id ORDER BY score DESC, unlocked DESC LIMIT ?",
            (external_id, _iso(since), limit or -1),
        )
        return [
            TopGame(
                name=row["name"],
                gamerscore=row["score"],
                unlocked=row["unlocked"],
                platform=row["platform"],
            )
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

    async def last_bot_message(self, chat_id: int) -> int | None:
        """For /delete_last (SPEC 6.4's follow-up) — Telegram message_ids are
        assigned sequentially per chat, so the highest one logged here *is*
        the most recent, no timestamp-tie ambiguity the way sent_at alone
        would have (same-second messages are common right after a poll tick
        publishes more than one)."""
        cursor = await self._conn.execute(
            "SELECT message_id FROM bot_messages WHERE chat_id = ? "
            "ORDER BY message_id DESC LIMIT 1",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

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

    async def upsert_title(
        self, title_id: str, name: str, platform: str | None, icon_url: str | None = None
    ) -> None:
        # icon_url only overwrites when this call actually has one —
        # ensure_title_name() (fetcher.py) upserts just the name/platform on
        # every new title it resolves, and must not blank out an icon_url a
        # separate ensure_title_icon() call already cached here.
        await self._conn.execute(
            "INSERT INTO titles (title_id, name, platform, icon_url, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(title_id) DO UPDATE SET name = excluded.name,"
            " platform = excluded.platform, updated_at = excluded.updated_at,"
            " icon_url = COALESCE(excluded.icon_url, titles.icon_url)",
            (title_id, name, platform, icon_url, utcnow_iso()),
        )
        await self._conn.commit()

    async def title_name(self, title_id: str) -> str | None:
        cursor = await self._conn.execute("SELECT name FROM titles WHERE title_id = ?", (title_id,))
        row = await cursor.fetchone()
        return row["name"] if row else None

    async def title_icon_url(self, title_id: str) -> str | None:
        cursor = await self._conn.execute(
            "SELECT icon_url FROM titles WHERE title_id = ?", (title_id,)
        )
        row = await cursor.fetchone()
        return row["icon_url"] if row else None

    async def hltb_all_ids(self) -> list[int]:
        """For the one-off platforms backfill (scripts/backfill_hltb_platforms.py)
        — every id already cached, so it can be re-resolved with the field
        that didn't exist when it was first cached."""
        cursor = await self._conn.execute("SELECT hltb_id FROM hltb_cache")
        return [row[0] for row in await cursor.fetchall()]

    async def hltb_get_cached(self, hltb_id: int) -> HltbCacheRow | None:
        cursor = await self._conn.execute(
            "SELECT hltb_id, name, release_year, main_hours, extra_hours,"
            " completionist_hours, platforms, game_url, image_url, genre "
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
            game_url=row["game_url"],
            image_url=row["image_url"],
            genre=row["genre"],
        )

    async def hltb_cache_result(self, entry: HltbCacheRow) -> None:
        """Cached forever (SPEC 6.6) — only called once someone actually
        picks a search result, never for the rest of the candidate list."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO hltb_cache "
            "(hltb_id, name, release_year, main_hours, extra_hours, completionist_hours,"
            " platforms, game_url, image_url, genre, cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.hltb_id,
                entry.name,
                entry.release_year,
                entry.main_hours,
                entry.extra_hours,
                entry.completionist_hours,
                json.dumps(entry.platforms),
                entry.game_url,
                entry.image_url,
                entry.genre,
                utcnow_iso(),
            ),
        )
        await self._conn.commit()

    # ---------------------------------------------- Steam achievement cache

    async def steam_schema_get_cached(
        self, appid: str
    ) -> tuple[str | None, list[SteamSchemaAchievement]] | None:
        """The game's own achievement list — cached forever, one row per
        appid, never invalidated (SPEC 9, M-Steam-2b): a game's achievements
        don't change between polls the way unlock percentages do."""
        cursor = await self._conn.execute(
            "SELECT game_name, achievements FROM steam_schema_cache WHERE appid = ?",
            (appid,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        achievements = [
            SteamSchemaAchievement(
                apiname=item["apiname"], icon=item["icon"], hidden=item["hidden"]
            )
            for item in json.loads(row["achievements"])
        ]
        return row["game_name"], achievements

    async def steam_schema_cache_result(
        self, appid: str, game_name: str | None, achievements: list[SteamSchemaAchievement]
    ) -> None:
        blob = json.dumps(
            [{"apiname": a.apiname, "icon": a.icon, "hidden": a.hidden} for a in achievements]
        )
        await self._conn.execute(
            "INSERT OR REPLACE INTO steam_schema_cache (appid, game_name, achievements, cached_at) "
            "VALUES (?, ?, ?, ?)",
            (appid, game_name, blob, utcnow_iso()),
        )
        await self._conn.commit()

    async def steam_rarity_get_cached(self, appid: str) -> tuple[dict[str, float], str] | None:
        """Percentages plus their own cache timestamp — unlike the schema
        above, real percentages drift over time, so the caller (services/
        steam/achievements.py) decides whether `cached_at` is too old and
        needs a fresh fetch, this layer just reports what's there."""
        cursor = await self._conn.execute(
            "SELECT percentages, cached_at FROM steam_rarity_cache WHERE appid = ?",
            (appid,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return json.loads(row["percentages"]), row["cached_at"]

    async def steam_rarity_cache_result(self, appid: str, percentages: dict[str, float]) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO steam_rarity_cache (appid, percentages, cached_at) "
            "VALUES (?, ?, ?)",
            (appid, json.dumps(percentages), utcnow_iso()),
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

    async def update_platform_display_name(
        self, tg_id: int, platform: str, display_name: str
    ) -> None:
        """Opportunistic refresh only (SPEC 9, M-Steam-2c) — the presence
        poller already has a fresh persona name from the same batch call it
        used to update presence, so the panel/connect card doesn't drift
        stale between actual /connect_steam calls. A no-op if the link was
        removed in the meantime (no row to update)."""
        await self._conn.execute(
            "UPDATE platform_links SET display_name = ? WHERE tg_id = ? AND platform = ?",
            (display_name, tg_id, platform),
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
        tz_offset_min=row["tz_offset_min"],
    )


def _iso(moment: datetime) -> str:
    """Stored timestamps are UTC ISO strings truncated to seconds."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")
