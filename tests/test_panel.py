"""Panel text: Xbox/Steam login lines (SPEC 6.2, M-Steam-1)."""

from __future__ import annotations

from bot.db.repo import Repo
from bot.handlers.panel import render_panel

TG_ID = 1


async def test_no_accounts_shows_only_xbox_not_connected(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "someone")

    text, _markup = await render_panel(repo, TG_ID)

    assert "Вход XBOX: — не подключён" in text
    assert "Вход Steam" not in text


async def test_steam_linked_without_xbox_shows_both_lines(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "someone")
    await repo.link_platform_account(TG_ID, "steam", "76561197960287930", "Gabe")

    text, _markup = await render_panel(repo, TG_ID)

    assert "Вход XBOX: — не подключён" in text
    assert "Вход Steam: Gabe" in text


async def test_xbox_connected_without_steam_has_no_steam_line(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "someone")
    await repo.link_xbox_account(TG_ID, "xuid-1", "Igor", 1000)

    text, _markup = await render_panel(repo, TG_ID)

    assert "Вход XBOX:   " in text
    assert "Вход Steam" not in text


async def test_both_platforms_linked_show_both_lines(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "someone")
    await repo.link_xbox_account(TG_ID, "xuid-1", "Igor", 1000)
    await repo.link_platform_account(TG_ID, "steam", "76561197960287930", "Gabe")

    text, _markup = await render_panel(repo, TG_ID)

    assert "Вход XBOX:   " in text
    assert "Вход Steam:  Gabe" in text
