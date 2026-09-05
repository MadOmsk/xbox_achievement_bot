-- Moves rarity_mode off user_settings (one value for every chat) onto
-- subscriptions (one value per chat someone publishes to) — SPEC 9,
-- M-Steam-2e's follow-up. A close-friends chat and a big public one can
-- reasonably want different answers to "what's worth showing".
--
-- Does NOT backfill subscriptions.rarity_mode from the old user_settings
-- column: that column is already gone from schema.sql's own definition (a
-- fresh database's user_settings never has it — migrations 002/015 were
-- both edited earlier to stop assuming it does, same reasoning), and
-- SQLite resolves every column reference in a statement at prepare time
-- even inside a branch that would never run, so there is no safe
-- conditional way to read a column that might not exist depending on
-- whether this is a fresh database or a real upgrade. Every subscription
-- starts at the schema default, 'all' — checked live in production first:
-- only one person (of seven) had picked anything else ('rare'), fixed by
-- hand afterward rather than solved in SQL for one row.
--
-- The user_settings rebuild below drops rarity_mode there the same way
-- migration 015 already drops show_x360 (and, as of that same edit,
-- rarity_mode too, on a database where migration 002 never restored it) —
-- by simply never selecting it into the new table. Safe either way: a
-- fresh database's user_settings already lacks the column (rebuilding
-- into the identical shape again is a harmless no-op), a real one
-- (production, at the point this actually runs there) still has it and
-- this silently leaves it behind.
--
-- digest_threshold is ALSO not part of this rebuild (edited here after
-- migration 019 moved it off user_settings too, the same way, 2026-09-05)
-- — same reasoning again: schema.sql no longer creates it on a fresh
-- database, keeping it here would collide the moment this migration runs.
-- Migration 019 re-adds it to subscriptions afterward; this file only
-- drops it from user_settings a little earlier than that.

PRAGMA foreign_keys = OFF;

CREATE TABLE subscriptions_new (
    chat_id     INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    tg_id       INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    rarity_mode TEXT NOT NULL DEFAULT 'all'
                CHECK (rarity_mode IN ('all', 'rare', 'hidden')),
    PRIMARY KEY (chat_id, tg_id)
);

INSERT INTO subscriptions_new (chat_id, tg_id, created_at)
SELECT chat_id, tg_id, created_at FROM subscriptions;

DROP TABLE subscriptions;
ALTER TABLE subscriptions_new RENAME TO subscriptions;

CREATE TABLE user_settings_new (
    tg_id            INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    muted_title_ids  TEXT    NOT NULL DEFAULT '[]',
    tz_offset_min    INTEGER
);

INSERT INTO user_settings_new (tg_id, muted_title_ids, tz_offset_min)
SELECT tg_id, muted_title_ids, tz_offset_min FROM user_settings;

DROP TABLE user_settings;
ALTER TABLE user_settings_new RENAME TO user_settings;

PRAGMA foreign_keys = ON;
