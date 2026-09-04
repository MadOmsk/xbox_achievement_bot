-- Steam's own presence table (M-Steam-2c, SPEC 9). Brand-new table, not a
-- column added to an existing one, so plain CREATE TABLE IF NOT EXISTS is
-- safe here (same reasoning as migration 013) — there's no existing table
-- whose old shape could collide with it.

CREATE TABLE IF NOT EXISTS steam_presence_state (
    steam_id         TEXT PRIMARY KEY,
    persona_state    INTEGER,
    gameid           TEXT,
    game_name        TEXT,
    changed_at       TEXT,
    last_ach_poll_at TEXT,
    updated_at       TEXT
);
