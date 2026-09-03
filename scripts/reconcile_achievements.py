"""One-time reconciliation: pull the *complete* achievement history for every
already-connected user, the same way `Fetcher.backfill()` does for a brand new
connect (SPEC 5.4).

Why this is needed: before the reconnect fix, `seen_achievements` only grew
through live polling and `catch_up` — both bounded to games actually touched
since a person connected — so anything from a game they had not replayed
never landed there. Reconnecting from here on re-runs the full backfill and
closes that gap going forward, but everyone already connected is still
carrying the old gap until their *next* reconnect. This script closes it now,
once, instead of waiting.

Safe to run any time, including while the bot is live: `insert_new_achievements`
is `INSERT OR IGNORE` keyed on (xuid, title_id, achievement_id), and nothing
here ever publishes — every row goes in with is_backfill=True.

Usage:
    .venv/Scripts/python.exe -X utf8 -m scripts.reconcile_achievements
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from bot.config import get_settings
from bot.db.repo import Database, Repo
from bot.poller.fetcher import Fetcher
from bot.poller.publisher import Publisher
from bot.services.crypto import TokenCipher
from bot.services.xbox.auth import XboxAuthService
from bot.services.xbox.client import XboxApiError, XboxClient

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("reconcile")


async def main() -> None:
    settings = get_settings()
    database = await Database(settings.db_path).connect()
    repo = Repo(database)
    cipher = TokenCipher(settings.fernet_key.get_secret_value())
    auth = XboxAuthService(settings, repo, cipher)
    await auth.start()
    # Never actually sends anything: backfill() has no publish step, so the
    # Publisher instance here is wiring Fetcher expects, not something used.
    bot = Bot(token=settings.bot_token.get_secret_value())
    publisher = Publisher(bot, repo)
    client = XboxClient(auth)
    fetcher = Fetcher(repo, client, publisher, settings.backfill_concurrency)

    users = [u for u in await repo.admin_users() if u.xuid]
    log.info("reconciling %s connected users", len(users))

    for user in users:
        name = user.gamertag or f"id{user.tg_id}"
        try:
            _, before_score = await repo.achievement_counts(user.xuid, None)
            total = await fetcher.backfill(user.tg_id, user.xuid)
            _, after_score = await repo.achievement_counts(user.xuid, None)
        except XboxApiError as exc:
            log.warning("%s: skipped, %s", name, exc)
            continue
        gained = after_score - before_score
        log.info(
            "%s: %s achievements on record, +%s gamerscore recovered (%s -> %s)",
            name,
            total,
            gained,
            before_score,
            after_score,
        )

    await auth.close()
    await bot.session.close()
    await database.close()


if __name__ == "__main__":
    asyncio.run(main())
