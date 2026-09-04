-- Adds last_active_gameid/last_active_game_name/last_active_at to
-- steam_presence_state, for the Steam achievement poller's grace period
-- (SPEC 9's follow-up: found live, Steam's presence sometimes stops
-- reporting gameid for several minutes while someone keeps playing, and
-- achievement polling only runs for games the person is currently shown
-- playing — an achievement sat unseen for ~19 minutes because of exactly
-- this). Rebuilt via a new table and swap, not ALTER TABLE ADD COLUMN —
-- schema.sql already creates steam_presence_state with these columns for a
-- brand-new database, and SQLite has no "add column only if it doesn't
-- already exist" (same reasoning as migrations 002/006/010/012). No foreign
-- keys in or out of this table, so no PRAGMA toggle needed around the swap.

CREATE TABLE steam_presence_state_new (
    steam_id              TEXT PRIMARY KEY,
    persona_state         INTEGER,
    gameid                TEXT,
    game_name             TEXT,
    changed_at            TEXT,
    last_ach_poll_at      TEXT,
    updated_at            TEXT,
    last_active_gameid    TEXT,
    last_active_game_name TEXT,
    last_active_at        TEXT
);

INSERT INTO steam_presence_state_new
    (steam_id, persona_state, gameid, game_name, changed_at, last_ach_poll_at, updated_at,
     last_active_gameid, last_active_game_name, last_active_at)
SELECT
    steam_id, persona_state, gameid, game_name, changed_at, last_ach_poll_at, updated_at,
    -- Backfill from the row's own current gameid — the best available guess
    -- at "last confirmed active", short of remembering true history we never
    -- recorded. A stale/offline row backfills to NULL either way, which is
    -- exactly what a never-primed grace window should start as.
    gameid, game_name, CASE WHEN gameid IS NOT NULL THEN updated_at END
FROM steam_presence_state;

DROP TABLE steam_presence_state;
ALTER TABLE steam_presence_state_new RENAME TO steam_presence_state;
