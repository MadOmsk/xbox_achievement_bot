"""Inline keyboards shared by the connect flow and the panel."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Offsets, not zone names: MSK and CST are ambiguous, +03:00 is not (SPEC 6.1.1).
COMMON_OFFSETS_HOURS: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 9, 10)
ALL_OFFSETS_HOURS: tuple[int, ...] = tuple(range(-12, 15))

TZ_SET = "tz:set"
TZ_MORE = "tz:more"
TZ_SKIP = "tz:skip"


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
    if skippable:
        builder.row(InlineKeyboardButton(text="Пропустить", callback_data=TZ_SKIP))
    return builder.as_markup()


def connect_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Подключить Xbox", url=url)]]
    )


# "Never digest" is stored as a number rather than NULL so the publisher stays
# a single comparison: any real session is smaller than this.
DIGEST_NEVER = 99
DIGEST_CHOICES = (2, 3, 4, 5, 6, 8, 10, DIGEST_NEVER)


def format_digest(threshold: int) -> str:
    return "никогда" if threshold >= DIGEST_NEVER else f"от {threshold} ачивок"


# The One/Series/PC counterpart of the Xbox 360 switch below it: same three
# choices in spirit — show everything, show only the rare ones, or nothing.
RARITY_CHOICES = ("all", "rare", "hidden")


def format_rarity(mode: str, threshold: str | float) -> str:
    if mode == "hidden":
        return "не показывать"
    if mode == "rare":
        return f"только редкие (≤ {threshold}%)"
    return "любые"


def rarity_keyboard(current: str, threshold: str | float) -> InlineKeyboardMarkup:
    labels = {"all": "Любые", "rare": f"Только редкие (≤ {threshold}%)", "hidden": "Не показывать"}
    builder = InlineKeyboardBuilder()
    for mode in RARITY_CHOICES:
        mark = "• " if mode == current else ""
        builder.row(
            InlineKeyboardButton(text=f"{mark}{labels[mode]}", callback_data=f"panel:rarity:{mode}")
        )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="panel:refresh"))
    return builder.as_markup()


def panel_keyboard(
    tz_offset_min: int | None,
    rarity_mode: str = "all",
    rare_threshold: str | float = 10,
    show_x360: bool = True,
    digest_threshold: int = 3,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"One/Series/PC: {format_rarity(rarity_mode, rare_threshold)} ▸",
                    callback_data="panel:rarity",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Xbox 360: {'показывать' if show_x360 else 'не показывать'}",
                    callback_data="panel:x360",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Сводка: {format_digest(digest_threshold)} ▸",
                    callback_data="panel:digest",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Часовой пояс: {format_offset(tz_offset_min)} ▸",
                    callback_data="panel:tz",
                )
            ],
            [InlineKeyboardButton(text="🔄 Синхронизировать", callback_data="panel:sync")],
            [InlineKeyboardButton(text="Обновить", callback_data="panel:refresh")],
        ]
    )


def digest_keyboard(current: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in DIGEST_CHOICES:
        mark = "• " if value == current else ""
        label = "никогда" if value >= DIGEST_NEVER else str(value)
        builder.add(
            InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"panel:digest:{value}")
        )
    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data="panel:refresh"))
    return builder.as_markup()


def deep_link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть", url=url)]])
