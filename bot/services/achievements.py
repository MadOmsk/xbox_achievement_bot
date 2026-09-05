"""Filtering and wording of achievement messages (SPEC 5.5, 7.1, 7.2).

No I/O here: the poller decides when, this module decides whether and how.
"""

from __future__ import annotations

from html import escape as html_escape

from bot.db.repo import AchievementRow, ChatTarget
from bot.util import thousands

#  Two badges (2026-09-05 style pass) — a diamond for "редкая" (rare), a
#  cup for "обычная" (common). Every achievement gets one or the other,
#  including when there's no rarity_percent to judge by at all (Xbox 360,
#  backfilled rows) — unproven is not "rare", so it defaults to the cup
#  rather than going unbadged (found live: a whole game's worth of
#  achievements with no badge at all read as broken, not as "no data").
RARE_BADGE_MAX_PERCENT = 15.0


def rarity_badge(rarity_percent: float | None) -> str:
    if rarity_percent is not None and rarity_percent <= RARE_BADGE_MAX_PERCENT:
        return "💎"
    return "🏆"


# Same palette as /stats' and /online's platform circles (handlers/chat.py)
# — duplicated rather than imported, since services must never depend on
# handlers (CLAUDE.md's layering rule runs one way only). Labels are needed
# here and not there, so the two dicts aren't identical either.
_PLATFORM_ICON = {"modern": "🟢", "x360": "🟢", "steam": "⚫", "psn": "🔵"}
_PLATFORM_LABEL = {"modern": "XBOX", "x360": "XBOX 360", "steam": "Steam", "psn": "PlayStation"}


def platform_breakdown_suffix(xbox_count: int, steam_count: int) -> str:
    """The small "(🟢 3 · ⚫ 5)" next to a combined achievement total in
    /stats and /summary (2026-09-05 follow-up) — a parenthetical, not a
    second sort key or a second row: the combined number still leads and
    still sorts, this is purely for reference. Empty for anyone with
    achievements on only one platform in the window — nothing to break
    down, and showing it anyway would just be noise on every single-
    platform person's line."""
    parts = []
    if xbox_count:
        parts.append(f"{_PLATFORM_ICON['modern']} {xbox_count}")
    if steam_count:
        parts.append(f"{_PLATFORM_ICON['steam']} {steam_count}")
    if len(parts) < 2:
        return ""
    return " (" + " · ".join(parts) + ")"


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


def _spoiler(text: str, *, secret: bool) -> str:
    """Wraps already-escaped HTML text in a Telegram spoiler (SPEC 5.5, 7.1).

    Xbox's own isSecret does not redact name/description — found live, they
    carry the real, spoiler-containing text even while still locked. Hiding
    it from chat members who haven't unlocked (or don't want to know) it is
    entirely this bot's own doing, not something Microsoft did for us.
    """
    return f'<span class="tg-spoiler">{text}</span>' if secret else text


def _rarity_line(achievement: AchievementRow) -> str:
    """"(лого редкости)«название» · G · редкость %" — badge leads the name
    rather than trailing the percentage (standardized form, 2026-09-05
    follow-up); gamerscore appears only when it isn't 0, replacing the
    older platform-keyed rules (Steam/PSN got a hardcoded "no G" each) —
    zero is zero on any platform, no need to name which ones have none at
    all (SPEC 9, future platforms fall under this for free).
    """
    name = _spoiler(html_escape(achievement.name), secret=achievement.is_secret)
    name_part = f"{rarity_badge(achievement.rarity_percent)} «{name}»"

    tail = []
    if achievement.gamerscore:
        tail.append(f"{achievement.gamerscore} G")
    if achievement.rarity_percent is not None:
        tail.append(f"редкость {achievement.rarity_percent:g}%")
    return name_part if not tail else f"{name_part} · {' · '.join(tail)}"


def _game_line(title: str, platform: str) -> str:
    return f"{html_escape(title)} (<i>{platform_tag(platform)}</i>)"


def format_single(gamertag: str, achievement: AchievementRow, title_name: str | None) -> str:
    """Standardized form (2026-09-05 follow-up to SPEC 9, M-Steam-2e): one
    fixed wording regardless of platform — PSN can reopen the "трофей"
    wording once trophies are real data, not just a reserved word. Platform
    moved off the header (found there for one round, then judged noisier
    than useful) onto the game-title line, in italics, next to the game.
    """
    title = title_name or achievement.title_name or "неизвестная игра"
    header = f"<b>{html_escape(gamertag)}</b> получает достижение"
    game_line = _game_line(title, achievement.platform)
    text = f"{header}\n\n{game_line}\n{_rarity_line(achievement)}"
    if achievement.description:
        description = _spoiler(html_escape(achievement.description), secret=achievement.is_secret)
        text += f"\n\n{description}"
    return text


def _group_by_title(
    achievements: list[AchievementRow],
) -> dict[tuple[str, str], list[AchievementRow]]:
    """Keyed on (platform, title_id), not title_id alone — Xbox's and
    Steam's own id spaces (title_id vs. appid) don't promise to avoid each
    other. In practice every publish() call is already scoped to one game
    on one platform (poller/fetcher.py, poller/steam_fetcher.py each poll
    one title at a time) — this grouping exists so the digest renders
    correctly if that ever stops being true, not because it commonly sees
    more than one group today.
    """
    groups: dict[tuple[str, str], list[AchievementRow]] = {}
    for item in achievements:
        groups.setdefault((item.platform, item.title_id), []).append(item)
    return groups


def format_digest(gamertag: str, title_name: str | None, achievements: list[AchievementRow]) -> str:
    """Standardized form (2026-09-05 follow-up): one block per game, each
    shaped like format_single's own game+rarity lines — a digest reader who
    already knows the single-achievement layout should recognise this one.
    No total gamerscore in the header any more (same reasoning as
    _rarity_line dropping platform-specific "no G" rules): it used to add
    up to a real "+0 G" for an all-Steam session, which is exactly the kind
    of technically-true-but-misleading number the rest of this rework is
    getting rid of.

    Every achievement gets its own line, no "… и ещё N" cutoff (2026-09-05:
    dropped on request — a digest exists to say what happened, trimming it
    defeats that).
    """
    header = f"<b>{html_escape(gamertag)}</b> получает {plural_achievements(len(achievements))}"
    lines = [header, ""]
    for index, group in enumerate(_group_by_title(achievements).values()):
        if index > 0:
            lines.append("")  # a blank line between one game's block and the next
        title = group[0].title_name or title_name or "неизвестная игра"
        lines.append(_game_line(title, group[0].platform))
        lines.extend(_rarity_line(item) for item in group)
    return "\n".join(lines)


def plural_achievements(count: int) -> str:
    """"Достижение" everywhere, not "ачивка" — the two used to appear
    side by side across different messages (2026-09-05 terminology pass);
    "ач." stays fine as a space-saving abbreviation where one is needed,
    just not the full colloquial word."""
    tail = count % 10
    hundreds = count % 100
    number = thousands(count)
    if tail == 1 and hundreds != 11:
        return f"{number} достижение"
    if tail in (2, 3, 4) and hundreds not in (12, 13, 14):
        return f"{number} достижения"
    return f"{number} достижений"
