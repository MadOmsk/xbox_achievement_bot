-- Adds tg_id to seen_achievements and makes it the leading part of the
-- primary key, replacing xuid there (M-Steam-2, TODO.md and SPEC 9). A
-- person will soon have achievements from more than one platform, each
-- with its own external_id (Xbox xuid, Steam SteamID64) — summing "how
-- many ачивок across every platform" for one person needs one stable
-- per-person key, and tg_id is the only one that never changes per
-- platform. `xuid` stays as a plain column, not part of the key — still
-- the platform-specific external_id, just no longer what identifies whose
-- row this is. Also adds 'steam' to the platform CHECK, the actual point
-- of doing this now rather than when Steam achievement polling exists.
--
-- Backfilled via an INNER JOIN to the CURRENT users.xuid — checked live in
-- production first: every seen_achievements.xuid there matches a currently
-- -connected user, zero orphans. Note for future reference: this join
-- would silently drop rows for anyone who had achievements recorded and
-- later disconnected Xbox (users.xuid goes back to NULL on disconnect,
-- SPEC 6.1) with no other durable xuid->tg_id record anywhere. Not a risk
-- for this database today; would be if this file ever ran against an
-- older one with disconnected-and-reconnected users in its history.

PRAGMA foreign_keys = OFF;

CREATE TABLE seen_achievements_new (
    tg_id           INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
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
                    CHECK (platform IN ('modern', 'x360', 'steam')),
    is_backfill     INTEGER NOT NULL DEFAULT 0,
    is_secret       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tg_id, platform, title_id, achievement_id)
);

INSERT INTO seen_achievements_new
    (tg_id, xuid, title_id, achievement_id, name, description, icon_url, unlocked_at,
     gamerscore, rarity_percent, platform, is_backfill, is_secret, created_at)
SELECT
    u.tg_id, s.xuid, s.title_id, s.achievement_id, s.name, s.description, s.icon_url,
    s.unlocked_at, s.gamerscore, s.rarity_percent, s.platform, s.is_backfill, s.is_secret,
    s.created_at
FROM seen_achievements s
JOIN users u ON u.xuid = s.xuid;

DROP TABLE seen_achievements;
ALTER TABLE seen_achievements_new RENAME TO seen_achievements;

CREATE INDEX IF NOT EXISTS idx_seen_unlocked ON seen_achievements(xuid, unlocked_at DESC);
CREATE INDEX IF NOT EXISTS idx_seen_tg_unlocked ON seen_achievements(tg_id, unlocked_at DESC);

PRAGMA foreign_keys = ON;
