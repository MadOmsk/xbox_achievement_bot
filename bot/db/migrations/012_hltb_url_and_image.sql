-- Adds `game_url`/`image_url` to hltb_cache: the HLTB page link and cover
-- art for the game card (SPEC 6.6). Rebuilt via a new table and swap, not
-- ALTER TABLE ADD COLUMN, for the same reason as migration 006 — schema.sql
-- already creates hltb_cache with these columns for a brand-new database,
-- and a plain ALTER would collide there. No foreign keys in or out, so no
-- PRAGMA foreign_keys toggle needed around the swap.

CREATE TABLE hltb_cache_new (
    hltb_id             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    release_year        INTEGER,
    main_hours          REAL,
    extra_hours         REAL,
    completionist_hours REAL,
    platforms           TEXT NOT NULL DEFAULT '[]',
    game_url            TEXT,
    image_url           TEXT,
    cached_at           TEXT NOT NULL
);

INSERT INTO hltb_cache_new
    (hltb_id, name, release_year, main_hours, extra_hours, completionist_hours,
     platforms, cached_at)
SELECT hltb_id, name, release_year, main_hours, extra_hours, completionist_hours,
       platforms, cached_at
FROM hltb_cache;

DROP TABLE hltb_cache;
ALTER TABLE hltb_cache_new RENAME TO hltb_cache;
