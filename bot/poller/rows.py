"""`ParsedAchievement` → `AchievementRow` (2026-09-05 refactor).

Was duplicated byte-for-byte in fetcher.py and steam_fetcher.py. Lives here,
not in services/models.py: that module's own docstring is explicit about
`ParsedAchievement` never knowing about `bot.db.repo.AchievementRow` — the
conversion belongs "one layer up, in the poller" (SPEC 1.5's layering), just
shared between the two pollers that both need it now.
"""

from __future__ import annotations

from bot.db.repo import AchievementRow
from bot.services.models import ParsedAchievement


def to_achievement_row(item: ParsedAchievement) -> AchievementRow:
    return AchievementRow(
        title_id=item.title_id,
        achievement_id=item.achievement_id,
        name=item.name,
        description=item.description,
        icon_url=item.icon_url,
        unlocked_at=item.unlocked_at.isoformat(timespec="seconds") if item.unlocked_at else None,
        gamerscore=item.gamerscore,
        rarity_percent=item.rarity_percent,
        platform=item.platform,
        title_name=item.title_name,
        is_secret=item.is_secret,
    )
