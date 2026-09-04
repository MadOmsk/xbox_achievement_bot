-- Drops user_settings.show_x360 (M-Steam-2e, SPEC 1.4, 5.5): one rarity_mode
-- now governs every platform a person has connected, not a separate switch
-- per platform. A platform with no rarity_percent at all (currently only
-- Xbox 360) is exempt from the rarity check under 'rare' instead of having
-- its own visibility toggle — see services/achievements.py::passes_filters.
-- Rebuilt via a new table and swap — SQLite has no ALTER TABLE DROP COLUMN
-- before 3.35, and this project's own sqlite3 build predates relying on it
-- (same rebuild pattern as migration 002, which added show_x360 the same
-- way it's removed here).

PRAGMA foreign_keys = OFF;

CREATE TABLE user_settings_new (
    tg_id            INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
    rarity_mode      TEXT    NOT NULL DEFAULT 'all'
                     CHECK (rarity_mode IN ('all', 'rare', 'hidden')),
    digest_threshold INTEGER NOT NULL DEFAULT 3,
    muted_title_ids  TEXT    NOT NULL DEFAULT '[]',
    tz_offset_min    INTEGER
);

INSERT INTO user_settings_new
    (tg_id, rarity_mode, digest_threshold, muted_title_ids, tz_offset_min)
SELECT tg_id, rarity_mode, digest_threshold, muted_title_ids, tz_offset_min
FROM user_settings;

DROP TABLE user_settings;
ALTER TABLE user_settings_new RENAME TO user_settings;

PRAGMA foreign_keys = ON;
