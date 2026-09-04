"""Parsing of Xbox Live achievement responses (SPEC 4).

Three shapes arrive here and become one:

  contract 4 — a modern title, the only one that carries `rarity`;
  contract 2 — the whole library, used for backfill only (no rarity needed);
  contract 1 — Xbox 360, a completely different payload with no rarity at all.

Every block below is optional on purpose. Microsoft ships achievements without
`rarity`, without `rewards` and with an empty `mediaAssets`, and a missing block
must never cost us the achievement itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["modern", "x360"]

# "2025-08-30T09:17:58.7770000Z" — seven fractional digits, which
# datetime.fromisoformat refuses. Cut them down to six.
_FRACTION = re.compile(r"\.(\d{1,6})\d*")

# Microsoft has two "no date" markers: the zero date 0001-01-01 and 1753-01-01,
# the old SQL Server minimum (84 of 5239 rows on a live account). Xbox Live did
# not exist before 2005, so anything older than that is a placeholder, not a
# date — statistics must not count it as an unlock in the year 1753.
_EARLIEST_REAL_UNLOCK = datetime(2005, 1, 1, tzinfo=UTC)


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
    rarity_percent: float | None  # NULL for Xbox 360 — unknown, not "common"
    platform: Platform
    # Xbox's own isSecret. name/description are the real, spoiler-containing
    # text either way — Microsoft does not redact them for a still-locked
    # secret achievement, found live — hiding them under a Telegram spoiler
    # is entirely this bot's own doing (SPEC 5.5, 7.1).
    is_secret: bool = False


class _Rarity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    current_percentage: float | None = Field(default=None, alias="currentPercentage")


class _Progression(BaseModel):
    model_config = ConfigDict(extra="ignore")
    time_unlocked: str | None = Field(default=None, alias="timeUnlocked")


class _MediaAsset(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str | None = None
    url: str | None = None


class _Reward(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str | None = None
    value: str | int | None = None


class _TitleAssociation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int | str | None = None
    name: str | None = None


class ModernAchievement(BaseModel):
    """Contract 4 (and contract 2, which is the same minus `rarity`)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | int
    name: str = ""
    description: str | None = None
    progress_state: str | None = Field(default=None, alias="progressState")
    progression: _Progression | None = None
    rarity: _Rarity | None = None
    rewards: list[_Reward] = Field(default_factory=list)
    media_assets: list[_MediaAsset] = Field(default_factory=list, alias="mediaAssets")
    title_associations: list[_TitleAssociation] = Field(
        default_factory=list, alias="titleAssociations"
    )
    is_secret: bool = Field(default=False, alias="isSecret")

    @property
    def is_achieved(self) -> bool:
        """Only 'Achieved' counts (SPEC 5.3).

        Writing an InProgress row into seen_achievements would hide the
        achievement from publication forever.
        """
        return self.progress_state == "Achieved"

    def to_parsed(self, fallback_title_id: str | None = None) -> ParsedAchievement:
        association = self.title_associations[0] if self.title_associations else None
        title_id = str(association.id) if association and association.id else fallback_title_id
        return ParsedAchievement(
            achievement_id=str(self.id),
            title_id=title_id or "",
            title_name=association.name if association else None,
            name=self.name,
            description=self.description,
            icon_url=self._icon_url(),
            unlocked_at=parse_timestamp(
                self.progression.time_unlocked if self.progression else None
            ),
            gamerscore=self._gamerscore(),
            rarity_percent=self.rarity.current_percentage if self.rarity else None,
            platform="modern",
            is_secret=self.is_secret,
        )

    def _icon_url(self) -> str | None:
        for asset in self.media_assets:
            if asset.type == "Icon" and asset.url:
                return asset.url
        return None

    def _gamerscore(self) -> int:
        for reward in self.rewards:
            if reward.type == "Gamerscore" and reward.value is not None:
                try:
                    return int(reward.value)
                except (TypeError, ValueError):
                    return 0
        return 0


class X360Achievement(BaseModel):
    """Contract 1. No rarity exists for Xbox 360 and never will."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | int
    name: str = ""
    description: str | None = None
    gamerscore: int = 0
    unlocked: bool = False
    time_unlocked: str | None = Field(default=None, alias="timeUnlocked")
    title_id: int | str | None = Field(default=None, alias="titleId")

    @property
    def is_achieved(self) -> bool:
        return self.unlocked

    def to_parsed(self, fallback_title_id: str | None = None) -> ParsedAchievement:
        return ParsedAchievement(
            achievement_id=str(self.id),
            title_id=str(self.title_id) if self.title_id else (fallback_title_id or ""),
            title_name=None,  # contract 1 does not carry the title name
            name=self.name,
            description=self.description,
            icon_url=None,  # imageId is not a URL and the CDN pattern is undocumented
            unlocked_at=parse_timestamp(self.time_unlocked),
            gamerscore=self.gamerscore,
            rarity_percent=None,
            platform="x360",
        )


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1), value.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return None if parsed < _EARLIEST_REAL_UNLOCK else parsed


def parse_achievements(
    payload: dict[str, Any], platform: Platform, title_id: str | None = None
) -> list[ParsedAchievement]:
    """Turn a raw response into unlocked achievements only.

    Anything that fails to parse is skipped rather than raising: one malformed
    record must not cost a user his whole session.
    """
    model = X360Achievement if platform == "x360" else ModernAchievement
    result: list[ParsedAchievement] = []
    for item in payload.get("achievements") or []:
        try:
            achievement = model.model_validate(item)
        except Exception:
            continue
        if achievement.is_achieved:
            result.append(achievement.to_parsed(title_id))
    return result


def continuation_token(payload: dict[str, Any]) -> str | None:
    paging = payload.get("pagingInfo") or {}
    token = paging.get("continuationToken")
    return str(token) if token else None
