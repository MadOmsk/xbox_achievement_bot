"""Microsoft OAuth callback (SPEC 6.1.1). The only web surface in the project.

No UI beyond a "you can close this tab" page: everything else happens in
Telegram, and the browser is here only because Microsoft insists on a redirect.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from bot.config import Settings
from bot.services.connect import ConnectError, ConnectService
from bot.services.xbox.auth import TokenRefreshError, XboxIdentity

log = logging.getLogger(__name__)

OnLinked = Callable[[int, XboxIdentity, "int | None"], Awaitable[None]]

_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Xbox Achievement Bot</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; margin: 15vh auto; max-width: 30rem;
          padding: 0 1rem; text-align: center; }}
  .muted {{ color: #666; }}
</style>
<h1>{title}</h1>
<p class="muted">{text}</p>
"""


def _page(title: str, text: str, status: int = 200) -> web.Response:
    return web.Response(
        text=_PAGE.format(title=title, text=text), content_type="text/html", status=status
    )


class OAuthServer:
    def __init__(self, settings: Settings, connect: ConnectService, on_linked: OnLinked) -> None:
        self._settings = settings
        self._connect = connect
        self._on_linked = on_linked
        self._runner: web.AppRunner | None = None

    @property
    def _callback_path(self) -> str:
        # Taken from OAUTH_REDIRECT_URL so the route and what Azure knows
        # can never drift apart.
        return urlsplit(self._settings.oauth_redirect_url).path or "/auth/callback"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(self._callback_path, self._handle_callback)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self._settings.oauth_listen_host, self._settings.oauth_listen_port
        )
        await site.start()
        log.info(
            "oauth callback listening on %s:%s%s",
            self._settings.oauth_listen_host,
            self._settings.oauth_listen_port,
            self._callback_path,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_callback(self, request: web.Request) -> web.Response:
        error = request.query.get("error")
        if error:
            # The user pressed "Cancel" on the consent screen, or Microsoft
            # refused. error_description is for us, not for him.
            log.info(
                "consent screen returned error=%s: %s",
                error,
                request.query.get("error_description"),
            )
            return _page("Вход отменён", "Можно закрыть вкладку и попробовать снова в боте.")

        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return _page("Чего-то не хватает", "Открой ссылку из бота заново.", status=400)

        try:
            tg_id, identity, origin_chat_id = await self._connect.complete_login(state, code)
        except ConnectError as exc:
            return _page("Не получилось", str(exc), status=400)
        except TokenRefreshError:
            log.exception("token exchange failed")
            return _page("Microsoft не отдал токен", "Попробуй ещё раз: /connect", status=502)

        try:
            await self._on_linked(tg_id, identity, origin_chat_id)
        except Exception:
            # The account is already linked; only the Telegram message failed.
            log.exception("could not notify tg_id=%s about a successful login", tg_id)

        return _page(f"Готово, {identity.gamertag}", "Возвращайся в Telegram — там всё остальное.")
