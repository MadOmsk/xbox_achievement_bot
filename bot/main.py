"""Entry point: wires the database, Xbox auth, the OAuth callback and aiogram."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)

from bot.config import Settings, get_settings
from bot.db.repo import Database, Repo
from bot.handlers import admin as admin_handlers
from bot.handlers import chat as chat_handlers
from bot.handlers import connect as connect_handlers
from bot.handlers import panel as panel_handlers
from bot.handlers.chat import UsernameMiddleware
from bot.handlers.keyboards import timezone_keyboard
from bot.lock import AlreadyRunningError, single_instance
from bot.poller.daily import DailySummary
from bot.poller.fetcher import Fetcher
from bot.poller.presence import PresencePoller
from bot.poller.publisher import Publisher
from bot.poller.reminders import ReminderJob
from bot.poller.scheduler import PollerScheduler
from bot.services.connect import ConnectService
from bot.services.crypto import TokenCipher
from bot.services.notify import AdminNotifier
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

    notifier = AdminNotifier(bot, repo, settings.admin_tg_ids)
    auth.on_token_dead = notifier.token_dead

    client = XboxClient(auth)
    publisher = Publisher(bot, repo)
    fetcher = Fetcher(repo, client, publisher, settings.backfill_concurrency)
    poller = PresencePoller(settings, repo, client, fetcher)
    scheduler = PollerScheduler(
        poller, fetcher, ReminderJob(bot, repo), DailySummary(bot, repo), repo
    )

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

    async def refresh_after_reconnect(tg_id: int, xuid: str) -> None:
        """store_identity sets gamerscore = NULL on every connect (it does
        not know the real value yet) — for a brand-new account backfill()
        fixes that as a side effect, but a reconnect skips backfill entirely.
        Without this a person who logs back in sees "0" gamerscore until the
        next presence event happens to touch title_history (bug found live:
        justdrunkzero showed 0 right after reconnecting)."""
        try:
            await fetcher.refresh_title_history(tg_id, xuid)
        except Exception:
            log.exception("post-reconnect title history refresh failed for tg_id=%s", tg_id)

    async def on_linked(tg_id: int, identity: XboxIdentity) -> None:
        """Runs in the web callback, right after the account is stored."""
        # No achievements yet means this account is new to the bot, not someone
        # signing in again after his token expired.
        is_new = not await repo.has_any_achievements(identity.xuid)
        await bot.send_message(tg_id, f"✅ Подключил Xbox: {identity.gamertag}")
        await notifier.user_connected(tg_id, identity.gamertag, is_new=is_new)
        settings_row = await repo.get_user_settings(tg_id)
        if settings_row is None or settings_row.tz_offset_min is None:
            await bot.send_message(
                tg_id, connect_handlers.TIMEZONE_PROMPT, reply_markup=timezone_keyboard()
            )
        if is_new:
            await bot.send_message(tg_id, "Читаю твою историю ачивок, это займёт минуту…")
            asyncio.create_task(backfill(tg_id, identity.xuid))  # noqa: RUF006
        else:
            # A silent background refresh would leave the panel showing a
            # stale 0 for a few seconds with nothing telling the user why.
            await bot.send_message(tg_id, "Обновляю статистику…")
            asyncio.create_task(refresh_after_reconnect(tg_id, identity.xuid))  # noqa: RUF006

    web_server = OAuthServer(settings, connect_service, on_linked)
    await web_server.start()

    dispatcher = Dispatcher()
    dispatcher["repo"] = repo
    dispatcher["connect"] = connect_service
    dispatcher["fetcher"] = fetcher
    dispatcher["settings"] = settings
    dispatcher["notifier"] = notifier
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

    await _publish_command_menu(bot)

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


async def _publish_command_menu(bot: Bot) -> None:
    """The command list Telegram shows behind the "/" button.

    Two scopes, because the useful commands differ: in a group nobody needs
    /connect, and in private nobody needs /top.
    """
    private = [
        BotCommand(command="panel", description="Моя панель и настройки"),
        BotCommand(command="connect", description="Подключить Xbox"),
        BotCommand(command="disconnect", description="Отключить Xbox"),
        BotCommand(command="stats", description="Моя статистика"),
        BotCommand(command="help", description="Что я умею"),
    ]
    group = [
        BotCommand(command="subscribe", description="Публиковать мои ачивки здесь"),
        BotCommand(command="unsubscribe", description="Перестать публиковать"),
        BotCommand(command="top", description="Таблица за месяц"),
        BotCommand(command="summary", description="Сводка за последние сутки"),
        BotCommand(command="recent", description="Последние ачивки чата"),
        BotCommand(command="stats", description="Статистика игрока"),
        BotCommand(command="compare", description="Сравнить двоих"),
        BotCommand(command="help", description="Что я умею"),
    ]
    try:
        await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())
    except Exception:
        # A cosmetic menu is not worth failing the whole startup for.
        log.warning("could not publish the command menu", exc_info=True)


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
