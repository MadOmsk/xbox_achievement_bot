"""Steam login flow (Follow-up, 2026-09-05): the shared prompt_for_link()
step and the AwaitingSteamLink filter that lets any plain private message
finish it, both used by the panel button, the bare /connect_steam command,
and the group hub's deep link alike. The actual resolve+profile+link+
backfill body (_connect) is handler glue exercised live, same as the rest
of this project's aiogram handlers — not unit-tested directly."""

from __future__ import annotations

from types import SimpleNamespace

from aiogram import F
from pydantic import SecretStr

from bot.config import Settings
from bot.db.repo import Repo
from bot.handlers.steam import (
    _STEAM_LINK_PATTERN,
    AwaitingSteamLink,
    _awaiting_link,
    _unresolved_profile_hint,
    prompt_for_link,
)

TG_ID = 42


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


def _steam_settings(settings: Settings, *, configured: bool = True) -> Settings:
    key = SecretStr("fake-key") if configured else None
    return settings.model_copy(update={"steam_api_key": key})


def _event(tg_id: int | None) -> SimpleNamespace:
    user = SimpleNamespace(id=tg_id) if tg_id is not None else None
    return SimpleNamespace(from_user=user)


async def test_awaiting_filter_is_false_for_an_unarmed_user() -> None:
    _awaiting_link.discard(TG_ID)
    assert await AwaitingSteamLink()(_event(TG_ID)) is False


async def test_awaiting_filter_is_true_once_armed() -> None:
    _awaiting_link.add(TG_ID)
    try:
        assert await AwaitingSteamLink()(_event(TG_ID)) is True
    finally:
        _awaiting_link.discard(TG_ID)


async def test_awaiting_filter_is_false_with_no_user_at_all() -> None:
    # Defensive: some update types (e.g. a channel post) carry no from_user.
    assert await AwaitingSteamLink()(_event(None)) is False


def test_steam_link_pattern_matches_a_real_url_with_scheme() -> None:
    """Found live: the router's filter uses F.text.regexp's default MATCH
    mode, which is re.match — anchored at position 0 only. A pasted link
    always starts with "https://", so the pattern (which has no leading
    wildcard) never matched a single real link; only a bare
    "steamcommunity.com/..." with the scheme stripped off would have. Must
    use mode="search", exactly as steam.py's router registration does."""
    magic = F.text.regexp(_STEAM_LINK_PATTERN, mode="search")
    event = SimpleNamespace(text="https://steamcommunity.com/id/oiwio")
    assert magic.resolve(event)


def test_unresolved_hint_adds_the_nickname_explanation_for_a_bare_word() -> None:
    """2026-09-05 follow-up: Steam has no way to search by display name,
    only by the profile's custom URL slug — a bare word that isn't a link
    gets told this explicitly instead of just "not found"."""
    hint = _unresolved_profile_hint("CoolGamer123")
    assert "не по имени в клиенте" in hint
    assert "steamcommunity.com/id/gaben" in hint


def test_unresolved_hint_skips_the_nickname_explanation_for_a_real_link() -> None:
    """A real link that still failed to resolve (typo, deleted profile) —
    the nickname caveat would be misleading noise here."""
    hint = _unresolved_profile_hint("https://steamcommunity.com/id/typo123")
    assert "не по имени в клиенте" not in hint


async def test_prompt_replies_not_configured_without_arming(
    repo: Repo, settings: Settings
) -> None:
    bot = FakeBot()
    _awaiting_link.discard(TG_ID)
    unconfigured = _steam_settings(settings, configured=False)

    await prompt_for_link(bot, repo, unconfigured, TG_ID)  # type: ignore[arg-type]

    assert bot.sent == [
        (TG_ID, "Подключение Steam пока не настроено — обратитесь к администратору.")
    ]
    assert TG_ID not in _awaiting_link


async def test_prompt_reports_already_connected_without_arming(
    repo: Repo, settings: Settings
) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await repo.link_platform_account(TG_ID, "steam", "76561197960287930", "Gabe")
    bot = FakeBot()
    _awaiting_link.discard(TG_ID)

    await prompt_for_link(bot, repo, _steam_settings(settings), TG_ID)  # type: ignore[arg-type]

    assert bot.sent == [(TG_ID, "Steam уже подключён: Gabe.")]
    assert TG_ID not in _awaiting_link


async def test_prompt_arms_the_wait_and_sends_the_link_prompt(
    repo: Repo, settings: Settings
) -> None:
    await repo.ensure_user(TG_ID, "igor")
    bot = FakeBot()
    _awaiting_link.discard(TG_ID)

    await prompt_for_link(bot, repo, _steam_settings(settings), TG_ID)  # type: ignore[arg-type]

    assert TG_ID in _awaiting_link
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == TG_ID
    assert "steamcommunity.com" in text
    assert "публичной" in text  # the privacy warning is up front now, not just on failure
    _awaiting_link.discard(TG_ID)
