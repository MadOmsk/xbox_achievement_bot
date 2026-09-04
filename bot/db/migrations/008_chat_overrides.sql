-- Adds three per-chat overrides to chat_settings: rare_threshold_percent,
-- daily_summary_time, timezone (SPEC 5.5, 5.7) — each NULL by default,
-- meaning "follow the matching app_settings default". Rebuilt via a new
-- table and swap, not ALTER TABLE ADD COLUMN: SQLite has no "add column
-- only if it doesn't already exist" (same reasoning as migrations 002 and
-- 006). chat_settings has a foreign key to chats, so the swap needs
-- migration 002's PRAGMA foreign_keys toggle around it.
--
-- Does NOT carry `rarity_mode` forward on purpose: schema.sql no longer
-- creates chat_settings with that column at all (dropped in migration 009,
-- which runs right after this one) — for a brand-new database this table
-- never had it to begin with, so selecting it here would fail. Production,
-- which already ran this migration back when chat_settings still had
-- rarity_mode, is unaffected — this file only replays for a database that
-- hasn't reached version 008 yet.

PRAGMA foreign_keys = OFF;

CREATE TABLE chat_settings_new (
    chat_id                INTEGER PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    min_gamerscore          INTEGER NOT NULL DEFAULT 0,
    daily_summary           INTEGER NOT NULL DEFAULT 1,
    muted_title_ids         TEXT    NOT NULL DEFAULT '[]',
    rare_threshold_percent  REAL,
    daily_summary_time      TEXT,
    timezone                TEXT
);

INSERT INTO chat_settings_new
    (chat_id, min_gamerscore, daily_summary, muted_title_ids)
SELECT chat_id, min_gamerscore, daily_summary, muted_title_ids
FROM chat_settings;

DROP TABLE chat_settings;
ALTER TABLE chat_settings_new RENAME TO chat_settings;

PRAGMA foreign_keys = ON;
