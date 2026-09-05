-- Moves digest_threshold off user_settings (one value for every chat) onto
-- subscriptions (one value per chat someone publishes to) — same move and
-- same reasoning as migration 016's rarity_mode (Follow-up, 2026-09-05): a
-- quiet chat and a busy public one can reasonably want a different answer
-- to "how many achievements at once is a lot".
--
-- Does NOT backfill subscriptions.digest_threshold from the old
-- user_settings column, for the exact reason migration 016 already
-- documents: SQLite resolves every column reference in a statement at
-- prepare time even inside a branch that would never run, so there is no
-- safe conditional way to read a column that might not exist depending on
-- whether this is a fresh database or a real upgrade (and a fresh
-- database's user_settings never has this column once schema.sql drops
-- it). Every subscription starts at the schema default, 3 — checked live
-- in production first: only one person (of seven) had picked anything
-- else (2), fixed by hand afterward rather than solved in SQL for one row.
--
-- The user_settings rebuild below drops digest_threshold there the same
-- way migration 016 already drops rarity_mode — by simply never selecting
-- it into the new table.

PRAGMA foreign_keys = OFF;

CREATE TABLE subscriptions_new (
    chat_id          INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    tg_id            INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    created_at       TEXT NOT NULL,
    rarity_mode      TEXT NOT NULL DEFAULT 'all'
                     CHECK (rarity_mode IN ('all', 'rare', 'hidden')),
    digest_threshold INTEGER NOT NULL DEFAULT 3,
    PRIMARY KEY (chat_id, tg_id)
);

INSERT INTO subscriptions_new (chat_id, tg_id, created_at, rarity_mode)
SELECT chat_id, tg_id, created_at, rarity_mode FROM subscriptions;

DROP TABLE subscriptions;
ALTER TABLE subscriptions_new RENAME TO subscriptions;

CREATE TABLE user_settings_new (
    tg_id           INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    muted_title_ids TEXT    NOT NULL DEFAULT '[]',
    tz_offset_min   INTEGER
);

INSERT INTO user_settings_new (tg_id, muted_title_ids, tz_offset_min)
SELECT tg_id, muted_title_ids, tz_offset_min FROM user_settings;

DROP TABLE user_settings;
ALTER TABLE user_settings_new RENAME TO user_settings;

PRAGMA foreign_keys = ON;
