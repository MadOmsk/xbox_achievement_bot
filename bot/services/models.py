"""One unlocked achievement, in the shape every platform parser produces —
Xbox's (`services/xbox/models.py`) and Steam's (`services/steam/models.py`).

Lives here, not under either platform's own package, so a Steam parser
never has to import an "xbox" module for a type that isn't Xbox-specific,
and neither package needs to know about `bot.db.repo.AchievementRow` —
that conversion happens one layer up, in the poller (SPEC 1.5's layering:
a platform client never knows about Telegram or the database).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Platform = Literal["modern", "x360", "steam"]


@dataclass(slots=True)
class ParsedAchievement:
    """One unlocked achievement, in the shape the rest of the bot speaks."""

    achievement_id: str
    title_id: str
    title_name: str | None
    name: str
    description: str | None
    icon_url: str | None
    unlocked_at: datetime | None
    gamerscore: int
    rarity_percent: float | None  # NULL for Xbox 360 and Steam — unknown, not "common"
    platform: Platform
    # Xbox's own isSecret / Steam's own "hidden". name/description are the
    # real, spoiler-containing text either way — neither platform redacts
    # them for a still-locked secret achievement, found live for Xbox —
    # hiding them under a Telegram spoiler is entirely this bot's own doing
    # (SPEC 5.5, 7.1).
    is_secret: bool = False
