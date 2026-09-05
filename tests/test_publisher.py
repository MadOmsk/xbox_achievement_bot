"""poller/publisher.py's gallery building (SPEC 7.1/7.2)."""

from __future__ import annotations

from bot.db.repo import AchievementRow
from bot.poller.publisher import _gallery


def achievement(
    achievement_id: str,
    icon_url: str | None,
    is_secret: bool = False,
) -> AchievementRow:
    return AchievementRow(
        title_id="1",
        achievement_id=achievement_id,
        name="Ashes to Ashes",
        description=None,
        icon_url=icon_url,
        unlocked_at="2026-09-02T10:00:00+00:00",
        gamerscore=10,
        rarity_percent=None,
        platform="x360",
        is_secret=is_secret,
    )


def test_gallery_has_one_entry_per_achievement_with_distinct_icons() -> None:
    items = [achievement("a1", "url1"), achievement("a2", "url2")]
    assert _gallery(items) == [("url1", False), ("url2", False)]


def test_gallery_collapses_a_shared_icon_to_one_entry() -> None:
    """Xbox 360 achievements all share the game's own box art — a digest of
    several must not repeat that one picture once per achievement."""
    items = [achievement("a1", "boxart"), achievement("a2", "boxart"), achievement("a3", "boxart")]
    assert _gallery(items) == [("boxart", False)]


def test_gallery_skips_achievements_with_no_icon() -> None:
    items = [achievement("a1", None), achievement("a2", "url")]
    assert _gallery(items) == [("url", False)]


def test_gallery_is_empty_when_nothing_has_an_icon() -> None:
    assert _gallery([achievement("a1", None)]) == []


def test_gallery_marks_a_shared_icon_as_spoiler_if_any_sharer_is_secret() -> None:
    """A secret achievement sharing an already-seen icon must not ride in
    unmarked behind an earlier public one."""
    items = [
        achievement("a1", "boxart", is_secret=False),
        achievement("a2", "boxart", is_secret=True),
    ]
    assert _gallery(items) == [("boxart", True)]
