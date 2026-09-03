-- One person, several platform accounts (M-Steam-1, TODO.md) — Xbox stays in
-- users.xuid unchanged, this is only for accounts beyond it. No tokens here
-- on purpose: unlike Xbox, Steam's public data needs no per-user OAuth, just
-- one API key for the whole bot plus the person's own profile set to public.
CREATE TABLE IF NOT EXISTS platform_links (
    tg_id        INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    platform     TEXT    NOT NULL CHECK (platform IN ('steam', 'psn')),
    external_id  TEXT    NOT NULL,
    display_name TEXT,
    linked_at    TEXT    NOT NULL,
    PRIMARY KEY (tg_id, platform)
);
