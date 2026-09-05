"""Inline keyboards shared by the connect flow and the panel — and, same as
format_offset/format_rarity below, small handler-side helpers with no other
natural home. safe_edit (2026-09-05 refactor) is one of those: imported by
panel.py, connect.py, steam.py and hltb.py alike, none of which import each
other back through this module, so it can live wherever without risking a
cycle — this file already sits underneath all of them.
"""

from __future__ import annotations

import contextlib
from urllib.parse import quote

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder


def xbox_profile_url(gamertag: str) -> str:
    """The classic public gamer-profile page — works with no login, unlike
    the newer xbox.com/play/user/ page which redirects through a sign-in
    wall for a visitor who isn't signed in themselves (2026-09-05
    follow-up, panel's own "👤 Профиль" button)."""
    return f"https://account.xbox.com/en-us/profile?gamertag={quote(gamertag)}"


def steam_profile_url(steam_id: str) -> str:
    """The SteamID64 form always works, unlike a vanity URL — not every
    account has customized one (2026-09-05 follow-up)."""
    return f"https://steamcommunity.com/profiles/{steam_id}"


async def safe_edit(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None, **kwargs: object
) -> None:
    """Edit the callback's own message in place, tolerating the two routine
    failures every caller already needs to: the message isn't a real,
    editable Message (gone, or not accessible), or Telegram refuses an
    edit that changes nothing. Never calls callback.answer() itself —
    callers keep picking their own toast text, or none at all, same as
    before this existed."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup, **kwargs)


# Offsets, not zone names: MSK and CST are ambiguous, +03:00 is not (SPEC 6.1.1).
COMMON_OFFSETS_HOURS: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 9, 10)
ALL_OFFSETS_HOURS: tuple[int, ...] = tuple(range(-12, 15))

TZ_SET = "tz:set"
TZ_MORE = "tz:more"
TZ_SKIP = "tz:skip"
TZ_MANUAL = "tz:manual"


def format_offset(minutes: int | None) -> str:
    if minutes is None:
        return "по умолчанию"
    hours, rest = divmod(abs(minutes), 60)
    sign = "+" if minutes >= 0 else "−"
    return f"UTC{sign}{hours}" if rest == 0 else f"UTC{sign}{hours}:{rest:02d}"


def _offset_button(hours: int) -> InlineKeyboardButton:
    minutes = hours * 60
    return InlineKeyboardButton(text=format_offset(minutes), callback_data=f"{TZ_SET}:{minutes}")


def timezone_keyboard(*, full: bool = False, skippable: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    offsets = ALL_OFFSETS_HOURS if full else COMMON_OFFSETS_HOURS
    for hours in offsets:
        builder.add(_offset_button(hours))
    builder.adjust(4)

    if not full:
        builder.row(InlineKeyboardButton(text="Другой ▸", callback_data=TZ_MORE))
    # Faster than scrolling the full −12..+14 grid, and the only way to enter
    # a half-hour offset like +5:30 at all — the button grid only has whole
    # hours (SPEC 6.1.1).
    builder.row(InlineKeyboardButton(text="✏️ Ввести вручную", callback_data=TZ_MANUAL))
    if skippable:
        builder.row(InlineKeyboardButton(text="Пропустить", callback_data=TZ_SKIP))
    return builder.as_markup()


def connect_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Подключить XBOX", url=url)]]
    )


# "Never digest" is stored as a number rather than NULL so the publisher stays
# a single comparison: any real session is smaller than this.
DIGEST_NEVER = 99
DIGEST_CHOICES = (2, 3, 4, 5, 6, 8, 10, DIGEST_NEVER)


def format_digest(threshold: int) -> str:
    return "никогда" if threshold >= DIGEST_NEVER else f"от {threshold} достижений"


# One mode governs every connected platform at once within a given chat
# (SPEC 9, M-Steam-2e and its follow-up — per chat now, not one shared
# value for all of them, panel.py's "Мои чаты" chat card) — show
# everything, show only the rare ones, or nothing. A click cycles to the
# next one rather than opening a submenu — one tap, not two, for a
# three-way toggle. Used to have a separate Xbox 360 show/hide switch next
# to this one; folded in here instead of growing a second platform-specific
# toggle when Steam arrived (services/achievements.py::passes_filters).
RARITY_CHOICES = ("all", "rare", "hidden")


def format_rarity(mode: str) -> str:
    if mode == "hidden":
        return "не показывать"
    if mode == "rare":
        # No percentage here on purpose: the threshold is per-chat now (SPEC
        # 5.5), and this panel is not chat-scoped — a single number here
        # would only ever be right for one of possibly several chats.
        return "только редкие"
    return "любые"


def next_rarity_mode(current: str) -> str:
    index = RARITY_CHOICES.index(current) if current in RARITY_CHOICES else 0
    return RARITY_CHOICES[(index + 1) % len(RARITY_CHOICES)]


def disconnect_prompt_keyboard(*, from_panel: bool = False) -> InlineKeyboardMarkup:
    # Cancelling from the panel must restore the panel in place, not just
    # vanish — it needs its own callback so the handler knows to re-render
    # rather than delete the (only) message.
    cancel_data = "panel:disconnect:no" if from_panel else "disconnect:no"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, отключить", callback_data="disconnect:yes")],
            [InlineKeyboardButton(text="Отмена", callback_data=cancel_data)],
        ]
    )


STEAM_CONNECT_BUTTON = InlineKeyboardButton(
    text="🎮 Подключить Steam", callback_data="steam:connect"
)
STEAM_DISCONNECT_BUTTON = InlineKeyboardButton(
    text="🔕 Отключить Steam", callback_data="steam:disconnectprompt"
)


def _steam_button(*, steam_connected: bool) -> InlineKeyboardButton:
    return STEAM_DISCONNECT_BUTTON if steam_connected else STEAM_CONNECT_BUTTON


def panel_keyboard(
    tz_offset_min: int | None,
    *,
    connected: bool = True,
    needs_reconnect: bool = False,
    steam_connected: bool = False,
    gamertag: str | None = None,
    steam_id: str | None = None,
) -> InlineKeyboardMarkup:
    if not connected:
        # Xbox and Steam are independent (M-Steam-1) — someone with neither
        # connected yet should be offered both, not just Xbox first, and
        # someone with only Steam still gets a real disconnect option for
        # it rather than nothing at all.
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Подключить XBOX", callback_data="relogin")],
                [_steam_button(steam_connected=steam_connected)],
            ]
        )

    rows: list[list[InlineKeyboardButton]] = []
    if needs_reconnect:
        rows.append([InlineKeyboardButton(text="🔄 Подключить заново", callback_data="relogin")])
    if not steam_connected:
        rows.append([STEAM_CONNECT_BUTTON])
    rows += [
        [
            InlineKeyboardButton(
                text=f"Часовой пояс: {format_offset(tz_offset_min)} ▸",
                callback_data="panel:tz",
            )
        ],
        [InlineKeyboardButton(text="💬 Мои чаты ▸", callback_data="panel:chatlist")],
        [InlineKeyboardButton(text="🔄 Синхронизировать", callback_data="panel:sync")],
    ]
    # Profile link next to disconnect, one row each (2026-09-05 follow-up)
    # — gamertag/steam_id can in principle be missing (pre-first-sync edge
    # case), so the link only appears once there's something to link to.
    xbox_disconnect = InlineKeyboardButton(
        text="🔕 Отключить XBOX", callback_data="panel:disconnect"
    )
    rows.append(
        [InlineKeyboardButton(text="👤 Профиль", url=xbox_profile_url(gamertag)), xbox_disconnect]
        if gamertag
        else [xbox_disconnect]
    )
    # Symmetric with XBOX's own disconnect row above — the connect button
    # already moved up top when not connected, so the disconnect one sits
    # down here to match.
    if steam_connected:
        rows.append(
            [
                InlineKeyboardButton(text="👤 Профиль", url=steam_profile_url(steam_id)),
                STEAM_DISCONNECT_BUTTON,
            ]
            if steam_id
            else [STEAM_DISCONNECT_BUTTON]
        )
    rows.append([InlineKeyboardButton(text="Обновить", callback_data="panel:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def digest_keyboard(current: int, chat_id: int) -> InlineKeyboardMarkup:
    """Per chat now, not the main panel screen (Follow-up, 2026-09-05, same
    move as rarity_mode before it) — "Назад" goes back to that chat's own
    card, not the panel root."""
    builder = InlineKeyboardBuilder()
    for value in DIGEST_CHOICES:
        mark = "• " if value == current else ""
        label = "никогда" if value >= DIGEST_NEVER else str(value)
        builder.add(
            InlineKeyboardButton(
                text=f"{mark}{label}", callback_data=f"panel:cdigestset:{chat_id}:{value}"
            )
        )
    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=f"panel:chat:{chat_id}"))
    return builder.as_markup()


def deep_link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть", url=url)]])
