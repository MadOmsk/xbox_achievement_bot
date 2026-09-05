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
    chat_id     INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    tg_id       INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    -- Per (person, chat), not one global value on the person (moved off
    -- user_settings — same reasoning as 'all'/'rare'/'hidden' documented
    -- there originally, now scoped down: what counts as worth publishing
    -- can differ between a close-friends chat and a big public one).
    -- New subscriptions default to 'all', same as user_settings used to.
    rarity_mode TEXT NOT NULL DEFAULT 'all'
                CHECK (rarity_mode IN ('all', 'rare', 'hidden')),
    -- Per (person, chat), same move as rarity_mode above and for the same
    -- reason (Follow-up, 2026-09-05) — how many achievements at once
    -- deserve one summary message instead of separate ones can reasonably
    -- differ between a quiet chat and a busy one.
    digest_threshold INTEGER NOT NULL DEFAULT 3,
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
    -- rarity_mode and digest_threshold used to live here, one value for
    -- every chat a person publishes to. Both moved to subscriptions (one
    -- per chat, not one for all of them — Follow-ups, SPEC 9 M-Steam-2e
    -- and 2026-09-05) — a chat with close friends and a big public one can
    -- reasonably want different answers to both "what's worth showing" and
    -- "how many at once is a lot". rarity_mode is still one mode for every
    -- platform though (Xbox modern, Xbox 360, Steam — M-Steam-2e, SPEC
    -- 1.4): a platform with no rarity_percent at all (currently only Xbox
    -- 360) is exempt from the rarity check under 'rare' rather than
    -- getting a switch of its own.
    muted_title_ids  TEXT    NOT NULL DEFAULT '[]',
    tz_offset_min    INTEGER                       -- minutes from UTC, NULL = global timezone
);

-- Rare-achievement threshold, daily-summary time and its timezone are always
-- explicit per chat (SPEC 5.5, 5.7) — briefly shared via app_settings with a
-- NULL-means-"follow the global value" fallback, reverted once real multi-
-- chat use showed chats want genuinely different values, not one shared
-- knob that moves every chat at once on every edit. No *admin-controlled*
-- rarity mode column here — that used to gate publication alongside the
-- person's own choice (an AND of the two), dropped as redundant. The
-- person's own choice does live per chat, just not here: `subscriptions.
-- rarity_mode` (SPEC 9, M-Steam-2e's follow-up) — this table only supplies
-- the threshold number for what "rare" means once someone picks it.
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id                INTEGER PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    min_gamerscore          INTEGER NOT NULL DEFAULT 0,
    daily_summary           INTEGER NOT NULL DEFAULT 1,
    muted_title_ids         TEXT    NOT NULL DEFAULT '[]',
    rare_threshold_percent  REAL    NOT NULL DEFAULT 10,
    daily_summary_time      TEXT    NOT NULL DEFAULT '20:00',
    -- Offset, not a zone name — same reasoning as user_settings.tz_offset_min:
    -- unambiguous, and Russia has had no DST since 2014 so a fixed offset
    -- never drifts for this audience.
    tz_offset_min            INTEGER NOT NULL DEFAULT 180
);

-- Global settings the admin turns
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_by INTEGER,
    updated_at TEXT NOT NULL
);

-- Deduplication: what we have already seen. Keyed by tg_id, not by
-- xuid/external_id (M-Steam-2, TODO.md and SPEC 9): a person will soon have
-- achievements from more than one platform, each with its own external_id
-- (Xbox xuid, Steam SteamID64) — summing "how many across every platform"
-- for one person needs one stable per-person key, and tg_id is the only one
-- that never changes per platform. `xuid` stays as a plain column (not part
-- of the key) — still the platform-specific external_id, just no longer
-- what identifies whose row this is.
CREATE TABLE IF NOT EXISTS seen_achievements (
    tg_id           INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    xuid            TEXT NOT NULL,
    title_id        TEXT NOT NULL,
    achievement_id  TEXT NOT NULL,
    name            TEXT,
    description     TEXT,
    icon_url        TEXT,
    unlocked_at     TEXT,               -- UTC
    gamerscore      INTEGER,
    rarity_percent  REAL,               -- NULL on Xbox 360 and Steam
    platform        TEXT NOT NULL DEFAULT 'modern'
                    CHECK (platform IN ('modern', 'x360', 'steam')),
    is_backfill     INTEGER NOT NULL DEFAULT 0,  -- arrived via backfill, never published
    is_secret       INTEGER NOT NULL DEFAULT 0,  -- Xbox's own isSecret; name/description are
                                                  -- real either way, we're the ones who spoiler it
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tg_id, platform, title_id, achievement_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_unlocked ON seen_achievements(xuid, unlocked_at DESC);
-- idx_seen_tg_unlocked is NOT created here on purpose: _apply_schema() runs
-- this whole file via executescript on every startup, before migrations —
-- on a database that hasn't run 011 yet, `tg_id` doesn't exist on the
-- on-disk table, and this index would crash startup with "no such column:
-- tg_id" (hit for real in production, migration 009->011 upgrade). 011's
-- own rebuild-and-swap already (re)creates it, for both a fresh database
-- (011 still runs once, unconditionally, same as every migration) and an
-- upgraded one.

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

-- Steam's own presence (M-Steam-2c, SPEC 9) — separate table, not a reuse of
-- presence_state above: different shape (persona_state/gameid, not
-- state/title_id) and its own debounce, keyed by steam_id like the Xbox one
-- is keyed by xuid.
CREATE TABLE IF NOT EXISTS steam_presence_state (
    steam_id              TEXT PRIMARY KEY,
    persona_state         INTEGER,  -- Steam's own enum, 0=offline..6
    gameid                TEXT,     -- NULL when not in a game, right now
    game_name             TEXT,     -- gameextrainfo at the moment of the last change
    changed_at            TEXT,
    last_ach_poll_at      TEXT,
    updated_at            TEXT,
    -- Sticky through brief gaps, unlike gameid/game_name above: Steam's own
    -- presence intermittently stops reporting gameid for a few minutes even
    -- while someone keeps playing (found live — an achievement sat unseen
    -- for ~19 minutes because of exactly this). last_active_* remembers the
    -- last game we actually confirmed them in, and when, so the poller can
    -- keep checking it through a short gap instead of going idle
    -- (poller/steam_presence.py's grace period). Never read for /online —
    -- that still wants the raw, un-extended gameid above.
    last_active_gameid    TEXT,
    last_active_game_name TEXT,
    last_active_at        TEXT
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
    -- The game's own box art (titlehub's display_image), not an achievement
    -- icon — used as a stand-in icon for Xbox 360 achievement messages
    -- (fetcher.py's ensure_title_icon): contract 1 only ever gives a bare
    -- imageId int for an achievement, no documented way to turn it into a
    -- URL (verified live against the real API).
    icon_url   TEXT,
    updated_at TEXT NOT NULL
);

-- HowLongToBeat lookups (SPEC 6.6), keyed by HLTB's own game id — cached
-- forever once someone actually picks a search result.
CREATE TABLE IF NOT EXISTS hltb_cache (
    hltb_id             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    release_year        INTEGER,
    main_hours          REAL,
    extra_hours         REAL,
    completionist_hours REAL,
    platforms           TEXT NOT NULL DEFAULT '[]',  -- JSON list, HLTB's profile_platforms
    game_url            TEXT,  -- game_web_link — the HLTB page itself
    image_url           TEXT,  -- game_image_url — HLTB's own cover art
    genre               TEXT,  -- HLTB's own profile_genre, comma-separated as HLTB writes it
    cached_at           TEXT NOT NULL
);

-- Which chats already got their summary for a given day: the job wakes up every
-- minute, and without this a restart at the wrong moment would send it twice.
CREATE TABLE IF NOT EXISTS daily_reports (
    chat_id     INTEGER NOT NULL,
    report_date TEXT NOT NULL,   -- local date of the chat's timezone
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (chat_id, report_date)
);

-- Every message the bot has sent to a group chat (SPEC 6.4) — Telegram gives
-- a bot no way to list its own past messages, only to delete by message_id
-- one at a time, so without this log there is nothing for the admin panel's
-- "стереть сообщения бота" to delete. Logged by a request middleware
-- (bot/services/message_log.py), not scattered calls in every handler.
CREATE TABLE IF NOT EXISTS bot_messages (
    chat_id    INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

-- One person, several platform accounts (M-Steam-1, TODO.md) — Xbox stays in
-- users.xuid unchanged (nothing about it needs to change to add a second
-- platform), this is only for accounts beyond it. No tokens here on purpose:
-- unlike Xbox, Steam's public data needs no per-user OAuth, just one API key
-- for the whole bot plus the person's own profile visibility set to public.
CREATE TABLE IF NOT EXISTS platform_links (
    tg_id        INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    platform     TEXT    NOT NULL CHECK (platform IN ('steam', 'psn')),
    external_id  TEXT    NOT NULL,  -- SteamID64 / PSN account id
    display_name TEXT,              -- persona name / online ID, display cache
    linked_at    TEXT    NOT NULL,
    PRIMARY KEY (tg_id, platform)
);

-- Steam's own achievement schema for a game (M-Steam-2b, SPEC 9) — not about
-- any one person, one row per appid, JSON-blobbed like hltb_cache.platforms:
-- dozens of achievements per game, not worth a row-per-achievement table.
-- Cached forever, same as hltb_cache — a game's own achievement list/names/
-- icons/secrecy never change between polls.
CREATE TABLE IF NOT EXISTS steam_schema_cache (
    appid           TEXT PRIMARY KEY,
    game_name       TEXT,
    achievements    TEXT NOT NULL,   -- JSON: [{apiname, icon, hidden}] — name/description
                                      -- come from GetPlayerAchievements instead (already
                                      -- localized per person), not duplicated here
    cached_at       TEXT NOT NULL
);

-- Global unlock percentages per achievement (M-Steam-2b) — unlike the schema
-- above, the real percentage drifts over time as more people play, so this
-- one expires (bot/services/steam/achievements.py) instead of living forever.
CREATE TABLE IF NOT EXISTS steam_rarity_cache (
    appid           TEXT PRIMARY KEY,
    percentages     TEXT NOT NULL,   -- JSON: {apiname: percent}
    cached_at       TEXT NOT NULL
);
