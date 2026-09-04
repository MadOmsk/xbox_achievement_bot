-- Adds the two Steam achievement caches (M-Steam-2b, SPEC 9): schema (forever)
-- and global rarity (expiring). Brand-new tables, not a column added to an
-- existing one, so plain CREATE TABLE IF NOT EXISTS is safe here unlike
-- migrations 006/012 — there's no existing table whose old shape could
-- collide with it.

CREATE TABLE IF NOT EXISTS steam_schema_cache (
    appid           TEXT PRIMARY KEY,
    game_name       TEXT,
    achievements    TEXT NOT NULL,
    cached_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steam_rarity_cache (
    appid           TEXT PRIMARY KEY,
    percentages     TEXT NOT NULL,
    cached_at       TEXT NOT NULL
);
