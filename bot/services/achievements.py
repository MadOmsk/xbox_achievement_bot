"""Filtering and wording of achievement messages (SPEC 5.5, 7.1, 7.2).

No I/O here: the poller decides when, this module decides whether and how.
"""

from __future__ import annotations

from html import escape as html_escape

from bot.db.repo import AchievementRow, ChatTarget
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


# Same palette as /stats' and /online's platform circles (handlers/chat.py)
# — duplicated rather than imported, since services must never depend on
# handlers (CLAUDE.md's layering rule runs one way only). Labels are needed
# here and not there, so the two dicts aren't identical either.
_PLATFORM_ICON = {"modern": "🟢", "x360": "🟢", "steam": "⚫", "psn": "🔵"}
_PLATFORM_LABEL = {"modern": "Xbox", "x360": "Xbox 360", "steam": "Steam", "psn": "PlayStation"}


def platform_tag(platform: str) -> str:
    """SPEC 9, M-Steam-2e — which platform an achievement came from, right
    in the message itself, not just inferred from context. Found live: a
    Steam achievement arriving with no platform mention at all reads the
    same as any other message, easy to miss."""
    icon = _PLATFORM_ICON.get(platform, "⚪")
    label = _PLATFORM_LABEL.get(platform, platform)
    return f"{icon} {label}"


#  Platforms with no rarity data at all — the rarity filter can't decide
#  "rare" for them, so 'rare' mode falls back to showing everything, the
#  same way 'all' mode would (SPEC 5.5, 1.4). Currently only Xbox 360
#  (contract 1 never carries a rarity block); Steam is NOT here — it has a
#  real rarity_percent (GetGlobalAchievementPercentagesForApp, M-Steam-2b),
#  so it goes through the ordinary rarity check like modern Xbox.
_NO_RARITY_DATA_PLATFORMS = {"x360"}


def passes_filters(
    achievement: AchievementRow,
    chat: ChatTarget,
    rare_threshold: float,
) -> bool:
    """The person's own rarity choice for *this* chat, plus the chat's own
    spam guards.

    Rarity mode (all/rare/hidden) used to be one value for every chat a
    person publishes to (`user_settings.rarity_mode`) — moved to
    `subscriptions.rarity_mode`, one per chat (SPEC 9, M-Steam-2e's
    follow-up): a close-friends chat and a big public one can reasonably
    want different answers to "what's worth showing". A chat-*admin*-
    controlled rarity toggle used to exist too, gating this alongside the
    person's own choice — dropped once it turned out redundant, a second
    switch people had to find and agree on for no real benefit.

    One `rarity_mode`, not one per platform (SPEC 9, M-Steam-2e) — there
    used to be a separate `show_x360` switch here, folded into this single
    check when Steam arrived rather than growing a second platform-specific
    toggle to match it.
    """
    if chat.rarity_mode == "hidden":
        # Every platform's feed off entirely, for this chat.
        return False

    if achievement.platform not in _NO_RARITY_DATA_PLATFORMS and not _passes_rarity(
        achievement, chat.rarity_mode, rare_threshold
    ):
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


def format_single(gamertag: str, achievement: AchievementRow) -> str:
    """SPEC 9, M-Steam-2e — reworked so the platform is the headline, not an
    afterthought: which game this came from used to lead the second line,
    dropped in favor of the achievement's own name there, now that a
    message can come from either of two platforms and that's the more
    useful thing to know at a glance (the icon and the description below
    still say plenty about the specific game/achievement).
    """
    tag = platform_tag(achievement.platform)
    # "трофей" is PSN's own word for these, not built yet — this is the one
    # spot ready for it (SPEC 9, future): everything else about the message
    # (gamerscore vs. a trophy colour vs. nothing) already branches on
    # platform the same way.
    verb = "получает трофей" if achievement.platform == "psn" else "получает достижение"
    header = f"🏆 {html_escape(gamertag)} {verb} {tag}"

    name = _spoiler(html_escape(achievement.name), secret=achievement.is_secret)
    parts = [f"«{name}»"]
    # Steam has no gamerscore at all — services/steam/achievements.py always
    # parses it as 0, and "0 G" reads as a real (if trivial) score rather
    # than "not applicable here". PSN will show a trophy colour in this
    # same slot once it exists, neither score nor "nothing" (SPEC 9, future).
    if achievement.platform not in ("steam", "psn"):
        parts.append(f"{achievement.gamerscore} G")
    if achievement.platform != "x360" and achievement.rarity_percent is not None:
        badge = rarity_badge(achievement.rarity_percent)
        parts.append(f"редкость {achievement.rarity_percent:g}%{' ' + badge if badge else ''}")
    text = f"{header}\n{' · '.join(parts)}"
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
