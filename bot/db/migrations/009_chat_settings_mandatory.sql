-- Reverts migration 008's NULL-means-"follow the global value" design:
-- rare_threshold_percent, daily_summary_time and timezone become mandatory,
-- explicit per chat (SPEC 5.5, 5.7) — no shared fallback any more, decided
-- once real multi-chat use showed chats want genuinely different values,
-- not one shared knob. `timezone` (an IANA name) is also replaced by
-- `tz_offset_min` (a plain UTC offset in minutes), matching
-- user_settings.tz_offset_min — Russia has had no DST since 2014, so a
-- fixed offset is exact forever for this audience, not an approximation.
--
-- Also drops `rarity_mode`: it used to gate publication alongside the
-- user's own rarity_mode (an AND of the two) — redundant, since the user's
-- own choice already decides this; the chat only ever needed to supply the
-- threshold number. Any chat currently set to 'rare' loses that forced
-- gating on this migration — deliberate, not a bug: from here on every
-- user's own rarity choice is what decides, for every chat.
--
-- Every currently-NULL column is backfilled from whatever the global
-- app_settings value resolved to at the moment this migration runs, so an
-- existing chat's behavior does not silently change — only a brand new
-- chat gets the new hardcoded defaults (10%, 20:00, UTC+3) from here on.
-- IANA zone names are converted to a fixed offset with a plain CASE: every
-- name COMMON_ZONES has ever offered is a real, currently-serving, DST-free
-- Russian/UTC zone, so the mapping is exact, not a guess.

PRAGMA foreign_keys = OFF;

CREATE TABLE chat_settings_new (
    chat_id                INTEGER PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    min_gamerscore          INTEGER NOT NULL DEFAULT 0,
    daily_summary           INTEGER NOT NULL DEFAULT 1,
    muted_title_ids          TEXT    NOT NULL DEFAULT '[]',
    rare_threshold_percent  REAL    NOT NULL DEFAULT 10,
    daily_summary_time      TEXT    NOT NULL DEFAULT '20:00',
    tz_offset_min            INTEGER NOT NULL DEFAULT 180
);

INSERT INTO chat_settings_new
    (chat_id, min_gamerscore, daily_summary, muted_title_ids,
     rare_threshold_percent, daily_summary_time, tz_offset_min)
SELECT
    chat_id, min_gamerscore, daily_summary, muted_title_ids,
    COALESCE(
        rare_threshold_percent,
        (SELECT CAST(value AS REAL) FROM app_settings WHERE key = 'rare_threshold_percent'),
        10
    ),
    COALESCE(
        daily_summary_time,
        (SELECT value FROM app_settings WHERE key = 'daily_summary_time'),
        '20:00'
    ),
    COALESCE(
        CASE timezone
            WHEN 'Europe/Kaliningrad' THEN 120
            WHEN 'Europe/Moscow' THEN 180
            WHEN 'Asia/Yekaterinburg' THEN 300
            WHEN 'Asia/Omsk' THEN 360
            WHEN 'Asia/Krasnoyarsk' THEN 420
            WHEN 'Asia/Irkutsk' THEN 480
            WHEN 'Asia/Vladivostok' THEN 600
            WHEN 'UTC' THEN 0
            ELSE NULL
        END,
        (SELECT CASE value
            WHEN 'Europe/Kaliningrad' THEN 120
            WHEN 'Europe/Moscow' THEN 180
            WHEN 'Asia/Yekaterinburg' THEN 300
            WHEN 'Asia/Omsk' THEN 360
            WHEN 'Asia/Krasnoyarsk' THEN 420
            WHEN 'Asia/Irkutsk' THEN 480
            WHEN 'Asia/Vladivostok' THEN 600
            WHEN 'UTC' THEN 0
            ELSE 180
        END FROM app_settings WHERE key = 'timezone'),
        180
    )
FROM chat_settings;

DROP TABLE chat_settings;
ALTER TABLE chat_settings_new RENAME TO chat_settings;

DELETE FROM app_settings WHERE key IN ('rare_threshold_percent', 'daily_summary_time', 'timezone');

PRAGMA foreign_keys = ON;
