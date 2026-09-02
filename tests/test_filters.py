"""Publication filters (SPEC 5.5)."""

from __future__ import annotations

import pytest

from bot.db.repo import AchievementRow, ChatTarget, UserSettings
from bot.services.achievements import (
    format_digest,
    format_single,
    passes_filters,
    platform_note,
)


def achievement(
    rarity: float | None = 50.0, platform: str = "modern", gamerscore: int = 20
) -> AchievementRow:
    return AchievementRow(
        title_id="1",
        achievement_id="a1",
        name="Ashes to Ashes",
        description="Kill 100 enemies",
        icon_url=None,
        unlocked_at="2026-09-02T10:00:00+00:00",
        gamerscore=gamerscore,
        rarity_percent=rarity,
        platform=platform,
        title_name="Halo Infinite",
    )


def user(rarity_mode: str = "all", show_x360: bool = True) -> UserSettings:
    return UserSettings(
        tg_id=1,
        rarity_mode=rarity_mode,
        show_x360=show_x360,
        digest_threshold=3,
        tz_offset_min=180,
    )


def chat(rarity_mode: str = "all", min_gamerscore: int = 0, muted: list[str] | None = None):
    return ChatTarget(
        chat_id=-100,
        title="Гейминг-чат",
        rarity_mode=rarity_mode,
        min_gamerscore=min_gamerscore,
        muted_title_ids=muted or [],
    )


@pytest.mark.parametrize(
    ("threshold", "rarity", "expected"),
    [(10.0, 2.4, True), (10.0, 10.0, True), (10.0, 10.1, False), (20.0, 15.0, True)],
)
def test_rarity_threshold_is_the_admin_setting(
    threshold: float, rarity: float, expected: bool
) -> None:
    """The 10% is a default, never a constant in the code (SPEC 1.4)."""
    assert passes_filters(achievement(rarity), user("rare"), chat(), threshold) is expected


def test_x360_passes_rare_mode_when_the_switch_is_on() -> None:
    """Rarity is unknown for Xbox 360, not "too common" — a filter it has no
    data for must not silently hide it (SPEC 5.5)."""
    item = achievement(rarity=None, platform="x360")
    assert passes_filters(item, user("rare", show_x360=True), chat("rare"), 10.0) is True


def test_x360_switch_off_hides_it_regardless_of_rarity_mode() -> None:
    item = achievement(rarity=None, platform="x360")
    assert passes_filters(item, user("all", show_x360=False), chat(), 10.0) is False


def test_chat_and_user_settings_are_combined_with_and() -> None:
    item = achievement(rarity=30.0)
    assert passes_filters(item, user("all"), chat("rare"), 10.0) is False
    assert passes_filters(item, user("rare"), chat("all"), 10.0) is False
    assert passes_filters(item, user("all"), chat("all"), 10.0) is True


def test_min_gamerscore_and_mute() -> None:
    assert passes_filters(achievement(gamerscore=5), user(), chat(min_gamerscore=10), 10.0) is False
    assert passes_filters(achievement(), user(), chat(muted=["1"]), 10.0) is False


def test_single_message_mentions_rarity_and_badge() -> None:
    text = format_single("Igor", achievement(rarity=2.4), "Halo Infinite")
    assert "Igor выбил «Ashes to Ashes»" in text
    assert "Halo Infinite · 20 G · редкость 2.4% 💎" in text


def test_single_message_for_x360_says_so_instead_of_rarity() -> None:
    text = format_single("Igor", achievement(rarity=None, platform="x360"), "Halo 3")
    assert "Halo 3 · 20 G · Xbox 360" in text
    assert "редкость" not in text


def test_digest_counts_and_trims() -> None:
    items = [achievement(rarity=r) for r in (2.4, 11.0, 34.0, 50.0, 60.0)]
    text = format_digest("Igor", "Halo Infinite", items)
    assert "5 ачивок за сессию (+100 G)" in text
    assert "… и ещё 2" in text


def test_platform_note_keys_on_platform_not_missing_rarity() -> None:
    """Backfilled rows come from contract 2, which carries no rarity — a modern
    game must not be labelled "Xbox 360" because of that."""
    assert platform_note("x360", None) == " · Xbox 360"
    assert platform_note("modern", 2.4) == " · 2.4%"
    assert platform_note("modern", None) == ""
