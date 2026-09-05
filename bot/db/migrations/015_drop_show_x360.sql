-- Drops user_settings.show_x360 (M-Steam-2e, SPEC 1.4, 5.5): one rarity_mode
-- now governs every platform a person has connected, not a separate switch
-- per platform. A platform with no rarity_percent at all (currently only
-- Xbox 360) is exempt from the rarity check under 'rare' instead of having
-- its own visibility toggle — see services/achievements.py::passes_filters.
-- Rebuilt via a new table and swap — SQLite has no ALTER TABLE DROP COLUMN
-- before 3.35, and this project's own sqlite3 build predates relying on it
-- (same rebuild pattern as migration 002, which added show_x360 the same
-- way it's removed here).
--
-- rarity_mode and digest_threshold are NOT part of this rebuild (edited
-- here after migration 016 moved rarity_mode off user_settings, and again
-- after migration 019 moved digest_threshold the same way — both onto
-- subscriptions, SPEC 9 M-Steam-2e's follow-up and 2026-09-05's):
-- schema.sql no longer creates either column on a brand-new database, so
-- keeping them here would collide the moment this migration runs on a
-- fresh install (same reasoning as this file's own show_x360 removal, and
-- migration 002's matching fix). Production applied this migration's
-- *original* form (both columns kept, show_x360 dropped) before 016/019
-- existed, so editing it now only changes what a fresh install sees.

PRAGMA foreign_keys = OFF;

CREATE TABLE user_settings_new (
    tg_id            INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    muted_title_ids  TEXT    NOT NULL DEFAULT '[]',
    tz_offset_min    INTEGER
);

INSERT INTO user_settings_new (tg_id, muted_title_ids, tz_offset_min)
SELECT tg_id, muted_title_ids, tz_offset_min
FROM user_settings;

DROP TABLE user_settings;
ALTER TABLE user_settings_new RENAME TO user_settings;

PRAGMA foreign_keys = ON;
