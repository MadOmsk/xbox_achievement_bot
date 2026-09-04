-- Adds `is_secret` to seen_achievements — Xbox Live's own isSecret flag on
-- the achievement (SPEC 5.5, 7.1). Rebuilt via a new table and swap, not
-- ALTER TABLE ADD COLUMN: schema.sql already creates seen_achievements with
-- this column for a brand-new database, and SQLite has no "add column only
-- if it doesn't already exist" (same reasoning as migrations 002 and 006).
-- No foreign keys on this table, so no PRAGMA toggle needed around the swap.
--
-- Found live: `isSecret` is real, but `name`/`description` are the actual
-- spoiler text either way — Xbox does NOT redact them for a still-locked
-- secret achievement, `lockedDescription` is a separate, often-unused field
-- some games never bother to fill in differently. The bot is what decides
-- to hide it, via a Telegram spoiler, not Microsoft.

CREATE TABLE seen_achievements_new (
    xuid            TEXT NOT NULL,
    title_id        TEXT NOT NULL,
    achievement_id  TEXT NOT NULL,
    name            TEXT,
    description     TEXT,
    icon_url        TEXT,
    unlocked_at     TEXT,
    gamerscore      INTEGER,
    rarity_percent  REAL,
    platform        TEXT NOT NULL DEFAULT 'modern'
                    CHECK (platform IN ('modern', 'x360')),
    is_backfill     INTEGER NOT NULL DEFAULT 0,
    is_secret       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (xuid, title_id, achievement_id)
);

INSERT INTO seen_achievements_new
    (xuid, title_id, achievement_id, name, description, icon_url, unlocked_at,
     gamerscore, rarity_percent, platform, is_backfill, created_at)
SELECT
    xuid, title_id, achievement_id, name, description, icon_url, unlocked_at,
    gamerscore, rarity_percent, platform, is_backfill, created_at
FROM seen_achievements;

DROP TABLE seen_achievements;
ALTER TABLE seen_achievements_new RENAME TO seen_achievements;

CREATE INDEX IF NOT EXISTS idx_seen_unlocked ON seen_achievements(xuid, unlocked_at DESC);
