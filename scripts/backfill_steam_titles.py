"""One-time reconciliation: cache a Steam game's name into `titles` for
every already-linked account (SPEC 9, follow-up 2026-09-05).

Why this is needed: `insert_new_achievements_steam()` only started caching a
game's name into `titles` on 2026-09-05 (db/repo.py) — Xbox's own fetcher had
done this from the start (`ensure_title_name`), Steam never had an
equivalent. Every Steam achievement stored before that fix has a title_id
with no matching `titles` row, so `/recent` and `/stats`' games table show
"без названия" for them, even after the JOIN-key bug itself (chat_recent()
was keyed on xuid, not tg_id) was fixed the same day. This script closes the
gap for what is already in the database — same reasoning and same pattern as
scripts/reconcile_achievements.py's own gap-closing for Xbox.

Uses GetOwnedGames (the official Web API, same call steam_fetcher.py's own
backfill() already makes), not the Store API — the Store API needs a
cc=US/cc=RU dance to avoid dropping games blocked in the Russian store
(TODO.md's own note on this, from the /hltb description research) and this
already-used endpoint has no such problem.

Safe to run any time, including while the bot is live: `upsert_title` is an
idempotent UPSERT keyed on title_id, and nothing here touches
seen_achievements at all.

Usage:
    .venv/Scripts/python.exe -X utf8 -m scripts.backfill_steam_titles
"""

from __future__ import annotations

import asyncio
import logging

from bot.config import get_settings
from bot.db.repo import Database, Repo
from bot.services.steam.client import SteamApiError, get_owned_games

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("backfill_steam_titles")


async def main() -> None:
    settings = get_settings()
    if settings.steam_api_key is None:
        log.info("Steam is not configured, nothing to do")
        return
    api_key = settings.steam_api_key.get_secret_value()

    database = await Database(settings.db_path).connect()
    repo = Repo(database)

    links = await repo.platform_links_all("steam")
    log.info("checking titles for %s linked Steam accounts", len(links))

    cached = 0
    for link in links:
        name = link.display_name or f"tg_id={link.tg_id}"
        try:
            games = await get_owned_games(api_key, link.external_id)
        except SteamApiError as exc:
            log.warning("%s: skipped, %s", name, exc)
            continue
        for game in games:
            await repo.upsert_title(game.appid, game.name, "steam")
        log.info("%s: checked %s owned games", name, len(games))
        cached += len(games)

    log.info("done: upserted up to %s title rows (existing ones just refreshed)", cached)
    await database.close()


if __name__ == "__main__":
    asyncio.run(main())
