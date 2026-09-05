-- Adds icon_url to titles — the game's own box art (titlehub's own
-- display_image), used as a stand-in achievement icon for Xbox 360
-- (fetcher.py's ensure_title_icon): contract 1's achievement payload only
-- ever carries a bare imageId int with no documented way to turn it into a
-- URL, verified live against the real API. Rebuilt via a new table and
-- swap, not ALTER TABLE ADD COLUMN — schema.sql already creates titles
-- with this column for a brand-new database, and SQLite has no "add column
-- only if it doesn't already exist" (same reasoning as migrations
-- 002/006/010/012/017). No foreign keys in or out of this table, so no
-- PRAGMA toggle needed around the swap.

CREATE TABLE titles_new (
    title_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    platform   TEXT,
    icon_url   TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO titles_new (title_id, name, platform, updated_at)
SELECT title_id, name, platform, updated_at FROM titles;

DROP TABLE titles;
ALTER TABLE titles_new RENAME TO titles;
