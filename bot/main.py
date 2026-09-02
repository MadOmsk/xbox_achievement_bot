"""Entry point: wires the database, Xbox auth, the OAuth callback and aiogram."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from bot.config import Settings, get_settings
from bot.db.repo import Database, Repo
from bot.handlers import admin as admin_handlers
from bot.handlers import chat as chat_handlers
from bot.handlers import connect as connect_handlers
from bot.handlers import panel as panel_handlers
from bot.handlers.chat import UsernameMiddleware
from bot.handlers.keyboards import timezone_keyboard
from bot.lock import AlreadyRunningError, single_instance
from bot.poller.fetcher import Fetcher
from bot.poller.presence import PresencePoller
from bot.poller.publisher import Publisher
from bot.poller.reminders import ReminderJob
from bot.poller.scheduler import PollerScheduler
from bot.services.connect import ConnectService
from bot.services.crypto import TokenCipher
from bot.services.xbox.auth import XboxAuthService, XboxIdentity
from bot.services.xbox.client import XboxClient
from bot.util import parse_iso
from bot.web.oauth import OAuthServer

log = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        # stdout, not the default stderr: manage.ps1 redirects the two streams
        # to different files, and ordinary progress in the error log is noise
        # that hides real tracebacks.
        stream=sys.stdout,
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

    client = XboxClient(auth)
    publisher = Publisher(bot, repo)
    fetcher = Fetcher(repo, client, publisher, settings.backfill_concurrency)
    poller = PresencePoller(settings, repo, client, fetcher)
    scheduler = PollerScheduler(poller, fetcher, ReminderJob(bot, repo), repo)

    async def backfill(tg_id: int, xuid: str) -> None:
        """Runs in the background: five people connecting one evening must not
        block the poller (SPEC 5.6)."""
        try:
            count = await fetcher.backfill(tg_id, xuid)
        except Exception:
            log.exception("backfill for tg_id=%s failed", tg_id)
            await bot.send_message(
                tg_id,
                "Не смог перечитать твою историю ачивок. Публикация пока выключена, "
                "чтобы не завалить чат — напиши /connect ещё раз чуть позже.",
            )
            return
        await bot.send_message(
            tg_id,
            f"Готово: перечитал {count} уже выбитых ачивок — в чат они не полетят. "
            "Дальше публикую только новые.",
        )

    async def on_linked(tg_id: int, identity: XboxIdentity) -> None:
        """Runs in the web callback, right after the account is stored."""
        await bot.send_message(tg_id, f"✅ Подключил Xbox: {identity.gamertag}")
        settings_row = await repo.get_user_settings(tg_id)
        if settings_row is None or settings_row.tz_offset_min is None:
            await bot.send_message(
                tg_id, connect_handlers.TIMEZONE_PROMPT, reply_markup=timezone_keyboard()
            )
        if not await repo.has_any_achievements(identity.xuid):
            await bot.send_message(tg_id, "Читаю твою историю ачивок, это займёт минуту…")
            asyncio.create_task(backfill(tg_id, identity.xuid))  # noqa: RUF006

    web_server = OAuthServer(settings, connect_service, on_linked)
    await web_server.start()

    dispatcher = Dispatcher()
    dispatcher["repo"] = repo
    dispatcher["connect"] = connect_service
    dispatcher["fetcher"] = fetcher
    dispatcher["settings"] = settings
    dispatcher.message.outer_middleware(UsernameMiddleware(repo))
    dispatcher.include_router(admin_handlers.router)
    dispatcher.include_router(connect_handlers.router)
    dispatcher.include_router(panel_handlers.router)
    dispatcher.include_router(chat_handlers.router)

    async def startup_catch_up() -> None:
        """Pick up what happened while the bot was down (SPEC 5.8).

        In the background: a restart must not wait for the network before it
        starts answering people.
        """
        for target in await repo.pollable_users():
            user = await repo.get_user(target.tg_id)
            try:
                await fetcher.catch_up(
                    target.tg_id,
                    target.xuid,
                    (user.gamertag if user else None) or "Игрок",
                    parse_iso(target.updated_at),
                    settings.catchup_publish_window_hours,
                    settings.catchup_max_titles,
                )
            except Exception:
                log.exception("catch-up for tg_id=%s failed", target.tg_id)

    await publisher.start()
    scheduler.start()
    asyncio.create_task(startup_catch_up())  # noqa: RUF006

    me = await bot.me()
    log.info("bot @%s is up", me.username)
    try:
        await dispatcher.start_polling(bot, handle_signals=False)
    finally:
        scheduler.shutdown()
        await publisher.stop()
        await web_server.stop()
        await auth.close()
        await bot.session.close()
        await database.close()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    lock_path = settings.db_path.parent / "bot.lock"
    try:
        with single_instance(lock_path):
            try:
                asyncio.run(run(settings))
            except (KeyboardInterrupt, SystemExit):
                log.info("stopped")
    except AlreadyRunningError:
        # Not a traceback: this is a normal thing to do by mistake.
        print(
            "Бот уже запущен — вторая копия не нужна.\n"
            "Два бота с одним токеном отбирают друг у друга сообщения Telegram.\n"
            "Состояние: manage.bat status",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
