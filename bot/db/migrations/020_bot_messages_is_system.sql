-- Adds is_system to bot_messages (2026-09-05 follow-up, "system message"
-- auto-delete) — rebuilt via a new table and swap, not ALTER TABLE ADD
-- COLUMN: schema.sql already creates bot_messages with this column for a
-- brand-new database, and every migration file runs unconditionally even
-- there (schema_migrations starts empty regardless), so a plain ADD COLUMN
-- would fail with "duplicate column name" on a fresh install (same
-- reasoning as migrations 002/006/010/012/017/018).
--
-- Every already-logged row becomes is_system = 1 (the column's own
-- default) — we don't know what category those historical sends were,
-- and defaulting to "system" (auto-delete eligible) is the fail-safe
-- direction: a message that should have stuck around just vanishes a
-- little early once, rather than a stale one lingering forever.

PRAGMA foreign_keys = OFF;

CREATE TABLE bot_messages_new (
    chat_id    INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    sent_at    TEXT NOT NULL,
    is_system  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, message_id)
);

INSERT INTO bot_messages_new (chat_id, message_id, sent_at)
SELECT chat_id, message_id, sent_at FROM bot_messages;

DROP TABLE bot_messages;
ALTER TABLE bot_messages_new RENAME TO bot_messages;

PRAGMA foreign_keys = ON;
