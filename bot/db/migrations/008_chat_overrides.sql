-- Adds three per-chat overrides to chat_settings: rare_threshold_percent,
-- daily_summary_time, timezone (SPEC 5.5, 5.7) — each NULL by default,
-- meaning "follow the matching app_settings default". Rebuilt via a new
-- table and swap, not ALTER TABLE ADD COLUMN: schema.sql already creates
-- chat_settings with these columns for a brand-new database, and SQLite has
-- no "add column only if it doesn't already exist" (same reasoning as
-- migrations 002 and 006). chat_settings has a foreign key to chats, so the
-- swap needs migration 002's PRAGMA foreign_keys toggle around it.

PRAGMA foreign_keys = OFF;

CREATE TABLE chat_settings_new (
    chat_id                INTEGER PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    rarity_mode             TEXT    NOT NULL DEFAULT 'all'
                            CHECK (rarity_mode IN ('all', 'rare')),
    min_gamerscore          INTEGER NOT NULL DEFAULT 0,
    daily_summary           INTEGER NOT NULL DEFAULT 1,
    muted_title_ids         TEXT    NOT NULL DEFAULT '[]',
    rare_threshold_percent  REAL,
    daily_summary_time      TEXT,
    timezone                TEXT
);

INSERT INTO chat_settings_new
    (chat_id, rarity_mode, min_gamerscore, daily_summary, muted_title_ids)
SELECT chat_id, rarity_mode, min_gamerscore, daily_summary, muted_title_ids
FROM chat_settings;

DROP TABLE chat_settings;
ALTER TABLE chat_settings_new RENAME TO chat_settings;

PRAGMA foreign_keys = ON;
