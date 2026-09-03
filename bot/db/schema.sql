-- Full schema, SPEC section 3. Applied once to an empty database; later changes
-- go to db/migrations/ so an existing bot.db is never recreated from scratch.

-- Telegram users linked to an Xbox account
CREATE TABLE IF NOT EXISTS users (
    tg_id           INTEGER PRIMARY KEY,
    username        TEXT,                 -- for /compare @user, refreshed on every message
    xuid            TEXT UNIQUE,          -- identity key, NOT the gamertag
    gamertag        TEXT,                 -- display cache, can change
    gamerscore      INTEGER,              -- cache, refreshed together with titleHistory
    is_excluded     INTEGER NOT NULL DEFAULT 0,
    excluded_by     INTEGER,
    excluded_at     TEXT,
    last_online_at  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- One user, one token. Refresh only; everything else lives in memory.
CREATE TABLE IF NOT EXISTS tokens (
    tg_id             INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    refresh_token_enc BLOB NOT NULL,      -- Fernet, NEVER logged
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'invalid', 'revoked')),
    -- active:  usable token
    -- invalid: Microsoft refused to refresh (SPEC 5.1.2), polling stopped
    -- revoked: the user opted out himself, no more reminders
    fail_count        INTEGER NOT NULL DEFAULT 0,
    last_refresh_at   TEXT,
    invalid_at        TEXT,
    notify_count      INTEGER NOT NULL DEFAULT 0,
    last_notified_at  TEXT,
    created_at        TEXT NOT NULL
);

-- Chats we post to
CREATE TABLE IF NOT EXISTS chats (
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT,
    is_active  INTEGER NOT NULL DEFAULT 1,  -- cleared when Telegram answers 403
    added_by   INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id    INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    tg_id      INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, tg_id)
);

-- Who's been seen writing in a chat, separately from `subscriptions` (who
-- chose to publish there) — /online lists this, not just publishers (SPEC 6.3).
CREATE TABLE IF NOT EXISTS chat_seen (
    chat_id      INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    tg_id        INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, tg_id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    tg_id            INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    -- 'hidden' is the One/Series/PC counterpart of show_x360: the modern
    -- achievement feed off entirely, not just filtered to the rare ones.
    rarity_mode      TEXT    NOT NULL DEFAULT 'all'
                     CHECK (rarity_mode IN ('all', 'rare', 'hidden')),
    show_x360        INTEGER NOT NULL DEFAULT 1,
    digest_threshold INTEGER NOT NULL DEFAULT 3,
    muted_title_ids  TEXT    NOT NULL DEFAULT '[]',
    tz_offset_min    INTEGER                       -- minutes from UTC, NULL = global timezone
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id         INTEGER PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    rarity_mode     TEXT    NOT NULL DEFAULT 'all'
                    CHECK (rarity_mode IN ('all', 'rare')),
    min_gamerscore  INTEGER NOT NULL DEFAULT 0,
    daily_summary   INTEGER NOT NULL DEFAULT 1,
    muted_title_ids TEXT    NOT NULL DEFAULT '[]'
);

-- Global settings the admin turns: rare_threshold_percent, daily_summary_time, timezone
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_by INTEGER,
    updated_at TEXT NOT NULL
);

-- Deduplication: what we have already seen
CREATE TABLE IF NOT EXISTS seen_achievements (
    xuid            TEXT NOT NULL,
    title_id        TEXT NOT NULL,
    achievement_id  TEXT NOT NULL,
    name            TEXT,
    description     TEXT,
    icon_url        TEXT,
    unlocked_at     TEXT,               -- UTC
    gamerscore      INTEGER,
    rarity_percent  REAL,               -- NULL on Xbox 360
    platform        TEXT NOT NULL DEFAULT 'modern'
                    CHECK (platform IN ('modern', 'x360')),
    is_backfill     INTEGER NOT NULL DEFAULT 0,  -- arrived via backfill, never published
    created_at      TEXT NOT NULL,
    PRIMARY KEY (xuid, title_id, achievement_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_unlocked ON seen_achievements(xuid, unlocked_at DESC);

-- What was actually published where. Separate from seen_achievements: a user can be
-- subscribed in two chats, and a failure in one must not lose the achievement in the other.
CREATE TABLE IF NOT EXISTS publications (
    chat_id        INTEGER NOT NULL,
    xuid           TEXT NOT NULL,
    title_id       TEXT NOT NULL,
    achievement_id TEXT NOT NULL,
    message_id     INTEGER,
    posted_at      TEXT NOT NULL,
    PRIMARY KEY (chat_id, xuid, title_id, achievement_id)
);

-- Presence state — the polling engine
CREATE TABLE IF NOT EXISTS presence_state (
    xuid             TEXT PRIMARY KEY,
    state            TEXT,     -- Online / Offline
    title_id         TEXT,
    title_name       TEXT,
    changed_at       TEXT,     -- when title_id or state last changed
    last_ach_poll_at TEXT,     -- when achievements were last fetched (debounce)
    updated_at       TEXT
);

-- Title history cache (for /stats, /compare, /top)
CREATE TABLE IF NOT EXISTS title_history (
    xuid                  TEXT NOT NULL,
    title_id              TEXT NOT NULL,
    current_gamerscore    INTEGER,
    max_gamerscore        INTEGER,
    achievements_unlocked INTEGER,
    achievements_total    INTEGER,
    minutes_played        INTEGER,
    last_played_at        TEXT,
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (xuid, title_id)
);

CREATE TABLE IF NOT EXISTS titles (
    title_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    platform   TEXT,               -- x360 / modern
    updated_at TEXT NOT NULL
);

-- Which chats already got their summary for a given day: the job wakes up every
-- minute, and without this a restart at the wrong moment would send it twice.
CREATE TABLE IF NOT EXISTS daily_reports (
    chat_id     INTEGER NOT NULL,
    report_date TEXT NOT NULL,   -- local date of the chat's timezone
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (chat_id, report_date)
);
