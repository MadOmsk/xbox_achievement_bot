-- /online's live-updating table (Follow-up 2026-09-05, poller/online_refresh.py).
-- Brand-new table, not a column added to an existing one, so plain
-- CREATE TABLE IF NOT EXISTS is safe here (same reasoning as migrations
-- 013/014) — there's no existing table whose old shape could collide with it.

CREATE TABLE IF NOT EXISTS online_auto_refresh (
    chat_id         INTEGER PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    message_id      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    last_updated_at TEXT NOT NULL
);
