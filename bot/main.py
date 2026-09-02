"""Entry point: wires the database, Xbox auth, the OAuth callback and aiogram."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from bot.config import Settings, get_settings
from bot.db.repo import Database, Repo
from bot.handlers import connect as connect_handlers
from bot.handlers import panel as panel_handlers
from bot.handlers.keyboards import timezone_keyboard
from bot.services.connect import ConnectService
from bot.services.crypto import TokenCipher
from bot.services.xbox.auth import XboxAuthService, XboxIdentity
from bot.web.oauth import OAuthServer

log = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def run(settings: Settings) -> None:
    database = await Database(settings.db_path).connect()
    repo = Repo(database)

    # The global timezone is a setting, not a constant: the admin can change it
    # later without touching .env. The value from the environment only seeds it.
    if await repo.get_app_setting("timezone") is None:
        await repo.set_app_setting("timezone", settings.tz)

    cipher = TokenCipher(settings.fernet_key.get_secret_value())
    auth = XboxAuthService(settings, repo, cipher)
    await auth.start()
    connect_service = ConnectService(auth, repo)

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(link_preview_is_disabled=True),
    )

    async def on_linked(tg_id: int, identity: XboxIdentity) -> None:
        """Runs in the web callback, right after the account is stored."""
        await bot.send_message(tg_id, f"✅ Подключил Xbox: {identity.gamertag}")
        settings_row = await repo.get_user_settings(tg_id)
        if settings_row is None or settings_row.tz_offset_min is None:
            await bot.send_message(
                tg_id, connect_handlers.TIMEZONE_PROMPT, reply_markup=timezone_keyboard()
            )

    web_server = OAuthServer(settings, connect_service, on_linked)
    await web_server.start()

    dispatcher = Dispatcher()
    dispatcher["repo"] = repo
    dispatcher["connect"] = connect_service
    dispatcher.include_router(connect_handlers.router)
    dispatcher.include_router(panel_handlers.router)

    me = await bot.me()
    log.info("bot @%s is up", me.username)
    try:
        await dispatcher.start_polling(bot, handle_signals=False)
    finally:
        await web_server.stop()
        await auth.close()
        await bot.session.close()
        await database.close()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        asyncio.run(run(settings))
    except (KeyboardInterrupt, SystemExit):
        log.info("stopped")


if __name__ == "__main__":
    main()
