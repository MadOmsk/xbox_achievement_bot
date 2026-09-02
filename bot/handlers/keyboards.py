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


def panel_keyboard(tz_offset_min: int | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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


def deep_link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть", url=url)]])
