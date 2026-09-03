"""`/hltb` — HowLongToBeat search, works in both DM and group chat (SPEC 6.6).

A session lives on the single message it edits through end to end (prompt →
results → card), keyed by (chat_id, message_id) — not by the asker, so
pagination and picking work for whoever taps the buttons, but a *typed*
reply only ever counts as that session's query from the person who actually
ran /hltb. In-memory only, like every other "must not survive a restart"
flow in this project (admin's rare-threshold input, ConnectService._pending).

Busy-chat safety: a typed query is accepted only as an explicit Telegram
reply to the live prompt message, checked by message id — not "whatever this
person types next". A group chat can have hundreds of unrelated messages
between /hltb and an answer; a reply is the only way to point at the right
one unambiguously.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db.repo import Repo
from bot.services.hltb import HltbError, HltbResult, resolve, search

log = logging.getLogger(__name__)

router = Router(name="hltb")

PAGE_SIZE = 5
RECENT_GAMES_LIMIT = 10
# Generous — the reply-to-message check is the real guard against a stray
# match, this is just a backstop against sessions piling up forever.
SESSION_TTL_SECONDS = 1800

UNAVAILABLE = "HowLongToBeat сейчас недоступен, попробуй позже."
SESSION_STALE = "Сессия устарела, начни заново — /hltb"


@dataclass
class _Session:
    asker_tg_id: int
    started_at: float
    recent_games: list[str] = field(default_factory=list)
    results: list[HltbResult] = field(default_factory=list)
    page: int = 0


_sessions: dict[tuple[int, int], _Session] = {}


def _alive(session: _Session) -> bool:
    return time.monotonic() - session.started_at < SESSION_TTL_SECONDS


@router.message(Command("hltb"))
async def hltb_command(message: Message, repo: Repo) -> None:
    if message.from_user is None:
        return
    # Only meaningful in a group: a DM's chat_id is the asker's own tg_id,
    # which never has subscriptions/chat_seen rows of its own.
    recent = await repo.chat_recent_games(message.chat.id, RECENT_GAMES_LIMIT)

    text = "Название игры? Точное не нужно — покажу варианты."
    if recent:
        text += "\nОтветь на это сообщение (реплаем) или выбери из недавних:"
    else:
        text += "\nОтветь на это сообщение (реплаем)."

    prompt = await message.answer(text, reply_markup=_recent_keyboard(recent) if recent else None)
    _sessions[(message.chat.id, prompt.message_id)] = _Session(
        asker_tg_id=message.from_user.id, started_at=time.monotonic(), recent_games=recent
    )


def _recent_keyboard(names: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"hltb:qr:{i}")]
            for i, name in enumerate(names)
        ]
    )


async def _is_awaited_reply(message: Message) -> bool:
    if message.from_user is None or not message.text or message.reply_to_message is None:
        return False
    session = _sessions.get((message.chat.id, message.reply_to_message.message_id))
    # Only while still waiting for a first query — once results are shown the
    # interaction is button-driven, a further reply to that message is noise.
    return (
        session is not None
        and session.asker_tg_id == message.from_user.id
        and not session.results
        and _alive(session)
    )


@router.message(_is_awaited_reply)
async def hltb_query(message: Message, bot: Bot) -> None:
    assert message.text is not None and message.reply_to_message is not None
    prompt_id = message.reply_to_message.message_id
    session = _sessions.get((message.chat.id, prompt_id))
    if session is None:  # pragma: no cover — filter already checked this
        return
    await _run_search(bot, message.chat.id, prompt_id, session, message.text)


@router.callback_query(F.data.startswith("hltb:qr:"))
async def hltb_recent_pick(callback: CallbackQuery, bot: Bot) -> None:
    if not isinstance(callback.message, Message):
        return
    key = (callback.message.chat.id, callback.message.message_id)
    session = _sessions.get(key)
    if session is None or not _alive(session):
        await callback.answer(SESSION_STALE, show_alert=True)
        return
    assert callback.data is not None
    idx = int(callback.data.rsplit(":", 1)[1])
    if idx >= len(session.recent_games):
        await callback.answer()
        return
    await callback.answer()
    await _run_search(bot, key[0], key[1], session, session.recent_games[idx])


async def _run_search(
    bot: Bot, chat_id: int, message_id: int, session: _Session, query: str
) -> None:
    try:
        results = await search(query)
    except HltbError:
        log.info("HLTB search failed for %r", query)
        await _edit(bot, chat_id, message_id, UNAVAILABLE, None)
        _sessions.pop((chat_id, message_id), None)
        return

    if not results:
        text = f"По «{query}» ничего не нашёл, попробуй иначе."
        await _edit(bot, chat_id, message_id, text, None)
        return  # keep the session alive — the same reply target still works

    session.results = results
    session.page = 0
    await _edit(bot, chat_id, message_id, "Что из этого?", _results_keyboard(results, 0))


def _results_keyboard(results: list[HltbResult], page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = results[start : start + PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(text=_label(r), callback_data=f"hltb:pick:{r.hltb_id}")]
        for r in chunk
    ]

    pages = -(-len(results) // PAGE_SIZE)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"hltb:page:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="hltb:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"hltb:page:{page + 1}"))
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _label(result: HltbResult) -> str:
    return f"{result.name} ({result.release_year})" if result.release_year else result.name


@router.callback_query(F.data == "hltb:noop")
async def hltb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("hltb:page:"))
async def hltb_page(callback: CallbackQuery, bot: Bot) -> None:
    if not isinstance(callback.message, Message):
        return
    key = (callback.message.chat.id, callback.message.message_id)
    session = _sessions.get(key)
    if session is None or not _alive(session):
        await callback.answer(SESSION_STALE, show_alert=True)
        return
    assert callback.data is not None
    session.page = int(callback.data.rsplit(":", 1)[1])
    await callback.answer()
    markup = _results_keyboard(session.results, session.page)
    await _edit(bot, key[0], key[1], "Что из этого?", markup)


@router.callback_query(F.data.startswith("hltb:pick:"))
async def hltb_pick(callback: CallbackQuery, repo: Repo, bot: Bot) -> None:
    if not isinstance(callback.message, Message):
        return
    assert callback.data is not None
    hltb_id = int(callback.data.rsplit(":", 1)[1])
    try:
        result = await resolve(repo, hltb_id)
    except HltbError:
        await callback.answer(UNAVAILABLE, show_alert=True)
        return
    await callback.answer()
    chat_id, message_id = callback.message.chat.id, callback.message.message_id
    _sessions.pop((chat_id, message_id), None)
    await _edit(bot, chat_id, message_id, _card(result), None, html=True)


def _card(result: HltbResult) -> str:
    def fmt(hours: float | None) -> str:
        return f"{hours:.1f} ч" if hours else "—"

    title = f"⏱ <b>{result.name}</b>"
    if result.release_year:
        title += f" ({result.release_year})"
    return (
        f"{title}\n\n"
        f"Основной сюжет:     {fmt(result.main_hours)}\n"
        f"Основной + доп.:    {fmt(result.extra_hours)}\n"
        f"Полное прохождение: {fmt(result.completionist_hours)}"
    )


async def _edit(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None,
    *,
    html: bool = False,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML if html else None,
        )
    except Exception:
        # Telegram rejects an edit that changes nothing, or the message could
        # have been deleted meanwhile — neither is worth crashing over.
        log.info("could not edit hltb message %s/%s", chat_id, message_id)
