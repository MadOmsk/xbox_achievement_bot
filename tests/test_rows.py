"""ParsedAchievement -> AchievementRow (2026-09-05 refactor) — was
duplicated byte-for-byte in fetcher.py and steam_fetcher.py, now shared."""

from __future__ import annotations

from datetime import UTC, datetime

from bot.poller.rows import to_achievement_row
from bot.services.models import ParsedAchievement


def _parsed(**overrides: object) -> ParsedAchievement:
    defaults: dict[str, object] = {
        "achievement_id": "a1",
        "title_id": "t1",
        "title_name": "Some Game",
        "name": "Achievement",
        "description": "Do the thing",
        "icon_url": "https://example.com/icon.png",
        "unlocked_at": datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC),
        "gamerscore": 50,
        "rarity_percent": 12.5,
        "platform": "modern",
        "is_secret": False,
    }
    defaults.update(overrides)
    return ParsedAchievement(**defaults)  # type: ignore[arg-type]


def test_converts_every_field_across() -> None:
    row = to_achievement_row(_parsed())
    assert row.title_id == "t1"
    assert row.achievement_id == "a1"
    assert row.name == "Achievement"
    assert row.description == "Do the thing"
    assert row.icon_url == "https://example.com/icon.png"
    assert row.unlocked_at == "2026-09-05T10:00:00+00:00"
    assert row.gamerscore == 50
    assert row.rarity_percent == 12.5
    assert row.platform == "modern"
    assert row.title_name == "Some Game"
    assert row.is_secret is False


def test_missing_unlocked_at_stays_none() -> None:
    """Undated rows exist (SPEC 5.4) — must not crash formatting a date
    that was never there."""
    row = to_achievement_row(_parsed(unlocked_at=None))
    assert row.unlocked_at is None


def test_secret_flag_carries_through() -> None:
    assert to_achievement_row(_parsed(is_secret=True)).is_secret is True
