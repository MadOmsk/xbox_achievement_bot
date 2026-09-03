"""One-time backfill: re-resolve every already-cached HLTB entry so it picks
up `platforms`, a field added to `hltb_cache` after those rows were first
written (SPEC 6.6) — they were cached with `platforms = []` by the migration
default, not because HLTB has no platform data for them.

Safe to re-run: `hltb_cache_result` is INSERT OR REPLACE keyed on hltb_id, so
this only overwrites existing rows with fresher data, never duplicates.

Usage:
    .venv/Scripts/python.exe -X utf8 -m scripts.backfill_hltb_platforms
"""

from __future__ import annotations

import asyncio
import logging

from howlongtobeatpy import HowLongToBeat

from bot.config import get_settings
from bot.db.repo import Database, HltbCacheRow, Repo
from bot.services.hltb import _as_result

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("hltb-backfill")

# Courtesy delay between requests to an endpoint with no official rate limit
# published — HLTB is not a service this project has any standing with.
REQUEST_DELAY_SECONDS = 1.0


async def main() -> None:
    settings = get_settings()
    database = await Database(settings.db_path).connect()
    repo = Repo(database)

    ids = await repo.hltb_all_ids()
    log.info("backfilling platforms for %s cached games", len(ids))

    done = 0
    for hltb_id in ids:
        try:
            entry = await HowLongToBeat().async_search_from_id(hltb_id)
        except Exception as exc:  # the library exposes no narrower exception type
            log.warning("hltb_id=%s: lookup failed, %s", hltb_id, exc)
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
            continue
        if entry is None:
            log.warning("hltb_id=%s: HLTB no longer has this entry, left as is", hltb_id)
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
            continue

        result = _as_result(entry)
        await repo.hltb_cache_result(
            HltbCacheRow(
                hltb_id=result.hltb_id,
                name=result.name,
                release_year=result.release_year,
                main_hours=result.main_hours,
                extra_hours=result.extra_hours,
                completionist_hours=result.completionist_hours,
                platforms=result.platforms,
            )
        )
        done += 1
        log.info("%s: %s -> %s", result.hltb_id, result.name, result.platforms or "нет данных")
        await asyncio.sleep(REQUEST_DELAY_SECONDS)

    log.info("done: %s/%s refreshed", done, len(ids))
    await database.close()


if __name__ == "__main__":
    asyncio.run(main())
