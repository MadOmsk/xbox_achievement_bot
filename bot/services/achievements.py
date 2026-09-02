"""Filtering and wording of achievement messages (SPEC 5.5, 7.1, 7.2).

No I/O here: the poller decides when, this module decides whether and how.
"""

from __future__ import annotations

from bot.db.repo import AchievementRow, ChatTarget, UserSettings

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
    """AND of the user's settings and the chat's — the stricter one wins.

    The chat protects itself from spam, the person protects himself from his
    own noise, and neither can loosen what the other tightened.
    """
    if achievement.platform == "x360" and not user.show_x360:
        return False

    if not _passes_rarity(achievement, user.rarity_mode, rare_threshold):
        return False
    if not _passes_rarity(achievement, chat.rarity_mode, rare_threshold):
        return False

    if achievement.gamerscore < chat.min_gamerscore:
        return False

    return achievement.title_id not in chat.muted_title_ids


def _passes_rarity(achievement: AchievementRow, mode: str, threshold: float) -> bool:
    if mode != "rare":
        return True
    if achievement.rarity_percent is None:
        # Xbox 360 has no rarity at all. Unknown is not "too common", so the
        # x360 switch decides its visibility, not this filter (SPEC 5.5).
        return achievement.platform == "x360"
    return achievement.rarity_percent <= threshold


def format_single(gamertag: str, achievement: AchievementRow, title_name: str | None) -> str:
    title = title_name or achievement.title_name or "неизвестная игра"
    parts = [title, f"{achievement.gamerscore} G"]
    if achievement.platform == "x360":
        parts.append("Xbox 360")
    elif achievement.rarity_percent is not None:
        badge = rarity_badge(achievement.rarity_percent)
        parts.append(f"редкость {achievement.rarity_percent:g}%{' ' + badge if badge else ''}")

    text = f"🏆 {gamertag} выбил «{achievement.name}»\n{' · '.join(parts)}"
    if achievement.description:
        text += f"\n\n{achievement.description}"
    return text


def format_digest(gamertag: str, title_name: str | None, achievements: list[AchievementRow]) -> str:
    title = title_name or next(
        (a.title_name for a in achievements if a.title_name), "неизвестная игра"
    )
    total_score = sum(a.gamerscore for a in achievements)
    header = f"🎮 {gamertag}, {title} — {_plural(len(achievements))} за сессию (+{total_score} G)"

    lines = [header, ""]
    for achievement in achievements[:DIGEST_PREVIEW]:
        badge = rarity_badge(achievement.rarity_percent) or "·"
        tail = (
            f" · {achievement.rarity_percent:g}%"
            if achievement.rarity_percent is not None
            else " · Xbox 360"
        )
        lines.append(f"{badge} {achievement.name} · {achievement.gamerscore} G{tail}")

    remaining = len(achievements) - DIGEST_PREVIEW
    if remaining > 0:
        lines.append(f"… и ещё {remaining}")
    return "\n".join(lines)


def _plural(count: int) -> str:
    tail = count % 10
    hundreds = count % 100
    if tail == 1 and hundreds != 11:
        return f"{count} ачивка"
    if tail in (2, 3, 4) and hundreds not in (12, 13, 14):
        return f"{count} ачивки"
    return f"{count} ачивок"
