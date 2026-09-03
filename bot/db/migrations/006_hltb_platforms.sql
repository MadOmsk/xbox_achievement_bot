-- Adds `platforms` to hltb_cache: HLTB's own profile_platforms (SPEC 6.6),
-- JSON-encoded like muted_title_ids elsewhere in this schema. Rebuilt via a
-- new table and swap, not ALTER TABLE ADD COLUMN — schema.sql already
-- creates hltb_cache with this column for a brand-new database, and SQLite
-- has no "add column only if it doesn't already exist", so a plain ALTER
-- would collide there (same reasoning as migration 002's rebuild of
-- user_settings). hltb_cache has no foreign keys in or out, so no need for
-- migration 002's PRAGMA foreign_keys toggle around the swap.

CREATE TABLE hltb_cache_new (
    hltb_id             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    release_year        INTEGER,
    main_hours          REAL,
    extra_hours         REAL,
    completionist_hours REAL,
    platforms           TEXT NOT NULL DEFAULT '[]',
    cached_at           TEXT NOT NULL
);

INSERT INTO hltb_cache_new
    (hltb_id, name, release_year, main_hours, extra_hours, completionist_hours, cached_at)
SELECT hltb_id, name, release_year, main_hours, extra_hours, completionist_hours, cached_at
FROM hltb_cache;

DROP TABLE hltb_cache;
ALTER TABLE hltb_cache_new RENAME TO hltb_cache;
