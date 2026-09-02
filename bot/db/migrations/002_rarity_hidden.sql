-- Adds a third state to user_settings.rarity_mode: 'hidden' — the
-- One/Series/PC counterpart of the Xbox 360 show/hide switch, instead of a
-- plain all/rare toggle. SQLite has no ALTER on a CHECK constraint, so the
-- table is rebuilt.

PRAGMA foreign_keys = OFF;

CREATE TABLE user_settings_new (
    tg_id            INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    rarity_mode      TEXT    NOT NULL DEFAULT 'all'
                     CHECK (rarity_mode IN ('all', 'rare', 'hidden')),
    show_x360        INTEGER NOT NULL DEFAULT 1,
    digest_threshold INTEGER NOT NULL DEFAULT 3,
    muted_title_ids  TEXT    NOT NULL DEFAULT '[]',
    tz_offset_min    INTEGER
);

INSERT INTO user_settings_new
    (tg_id, rarity_mode, show_x360, digest_threshold, muted_title_ids, tz_offset_min)
SELECT tg_id, rarity_mode, show_x360, digest_threshold, muted_title_ids, tz_offset_min
FROM user_settings;

DROP TABLE user_settings;
ALTER TABLE user_settings_new RENAME TO user_settings;

PRAGMA foreign_keys = ON;
