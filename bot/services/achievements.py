"""Filtering and wording of achievement messages (SPEC 5.5, 7.1, 7.2).

No I/O here: the poller decides when, this module decides whether and how.
"""

from __future__ import annotations

from html import escape as html_escape

from bot.db.repo import AchievementRow, ChatTarget, UserSettings
from bot.util import thousands

DIAMOND_MAX_PERCENT = 5.0
STAR_MAX_PERCENT = 15.0
DIGEST_PREVIEW = 3


def rarity_badge(rarity_percent: float | None) -> str:
    if rarity_percent is None:
        return ""
    if rarity_percent <= DIAMOND_MAX_PERCENT:
        return "💎"
    if rarity_percent <= STAR_MAX_PERCENT:
        return "⭐"
    return ""


def passes_filters(
    achievement: AchievementRow,
    user: UserSettings,
    chat: ChatTarget,
    rare_threshold: float,
) -> bool:
    """The user's own rarity choice, plus the chat's own spam guards.

    Rarity mode (all/rare/hidden) is the user's call alone — the chat only
    supplies the number that decides what "rare" means for it (SPEC 5.5); a
    chat-level rarity toggle used to gate this too, dropped once it turned
    out redundant with the user's own choice and just added a second switch
    people had to find and agree on.
    """
    if achievement.platform == "x360":
        # Xbox 360 has no rarity data at all — visibility is decided solely by
        # the show_x360 switch, never by the rarity filter below (SPEC 5.5).
        return user.show_x360

    if user.rarity_mode == "hidden":
        # The One/Series/PC counterpart of show_x360=0: the modern feed off
        # entirely.
        return False

    if not _passes_rarity(achievement, user.rarity_mode, rare_threshold):
        return False

    if achievement.gamerscore < chat.min_gamerscore:
        return False

    return achievement.title_id not in chat.muted_title_ids


def _passes_rarity(achievement: AchievementRow, mode: str, threshold: float) -> bool:
    if mode != "rare":
        return True
    if achievement.rarity_percent is None:
        return False
    return achievement.rarity_percent <= threshold


def platform_note(platform: str, rarity_percent: float | None) -> str:
    """The trailing " · 2.4%" of a line.

    Keyed on the platform, not on a missing rarity: backfilled rows come from
    contract 2, which carries no rarity at all, and a modern game must not be
    labelled "Xbox 360" because of that.
    """
    if platform == "x360":
        return " · Xbox 360"
    if rarity_percent is None:
        return ""
    return f" · {rarity_percent:g}%"


def _spoiler(text: str, *, secret: bool) -> str:
    """Wraps already-escaped HTML text in a Telegram spoiler (SPEC 5.5, 7.1).

    Xbox's own isSecret does not redact name/description — found live, they
    carry the real, spoiler-containing text even while still locked. Hiding
    it from chat members who haven't unlocked (or don't want to know) it is
    entirely this bot's own doing, not something Microsoft did for us.
    """
    return f'<span class="tg-spoiler">{text}</span>' if secret else text


def format_single(gamertag: str, achievement: AchievementRow, title_name: str | None) -> str:
    title = title_name or achievement.title_name or "неизвестная игра"
    parts = [html_escape(title), f"{achievement.gamerscore} G"]
    if achievement.platform == "x360":
        parts.append("Xbox 360")
    elif achievement.rarity_percent is not None:
        badge = rarity_badge(achievement.rarity_percent)
        parts.append(f"редкость {achievement.rarity_percent:g}%{' ' + badge if badge else ''}")

    name = _spoiler(html_escape(achievement.name), secret=achievement.is_secret)
    text = f"🏆 {html_escape(gamertag)} выбил «{name}»\n{' · '.join(parts)}"
    if achievement.description:
        description = _spoiler(html_escape(achievement.description), secret=achievement.is_secret)
        text += f"\n\n{description}"
    return text


def format_digest(gamertag: str, title_name: str | None, achievements: list[AchievementRow]) -> str:
    title = title_name or next(
        (a.title_name for a in achievements if a.title_name), "неизвестная игра"
    )
    total_score = sum(a.gamerscore for a in achievements)
    header = (
        f"🎮 {html_escape(gamertag)}, {html_escape(title)} — "
        f"{plural_achievements(len(achievements))} за сессию (+{total_score} G)"
    )

    lines = [header, ""]
    for achievement in achievements[:DIGEST_PREVIEW]:
        badge = rarity_badge(achievement.rarity_percent) or "·"
        tail = platform_note(achievement.platform, achievement.rarity_percent)
        name = _spoiler(html_escape(achievement.name), secret=achievement.is_secret)
        lines.append(f"{badge} {name} · {achievement.gamerscore} G{tail}")

    remaining = len(achievements) - DIGEST_PREVIEW
    if remaining > 0:
        lines.append(f"… и ещё {remaining}")
    return "\n".join(lines)


def plural_achievements(count: int) -> str:
    tail = count % 10
    hundreds = count % 100
    number = thousands(count)
    if tail == 1 and hundreds != 11:
        return f"{number} ачивка"
    if tail in (2, 3, 4) and hundreds not in (12, 13, 14):
        return f"{number} ачивки"
    return f"{number} ачивок"
