-- HowLongToBeat lookups (SPEC 6.6), keyed by HLTB's own game id — cached
-- forever once someone actually picks a search result.
CREATE TABLE IF NOT EXISTS hltb_cache (
    hltb_id             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    release_year        INTEGER,
    main_hours          REAL,
    extra_hours         REAL,
    completionist_hours REAL,
    cached_at           TEXT NOT NULL
);
