"""Publication filters (SPEC 5.5)."""

from __future__ import annotations

import pytest

from bot.db.repo import AchievementRow, ChatTarget
from bot.services.achievements import (
    format_digest,
    format_single,
    passes_filters,
    platform_note,
)


def achievement(
    rarity: float | None = 50.0,
    platform: str = "modern",
    gamerscore: int = 20,
    is_secret: bool = False,
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
        is_secret=is_secret,
    )


def chat(
    min_gamerscore: int = 0, muted: list[str] | None = None, rarity_mode: str = "all"
) -> ChatTarget:
    return ChatTarget(
        chat_id=-100,
        title="Гейминг-чат",
        min_gamerscore=min_gamerscore,
        muted_title_ids=muted or [],
        rare_threshold_percent=10.0,
        daily_summary_time="20:00",
        tz_offset_min=180,
        rarity_mode=rarity_mode,
    )


@pytest.mark.parametrize(
    ("threshold", "rarity", "expected"),
    [(10.0, 2.4, True), (10.0, 10.0, True), (10.0, 10.1, False), (20.0, 15.0, True)],
)
def test_rarity_threshold_is_the_chat_setting(
    threshold: float, rarity: float, expected: bool
) -> None:
    """The 10% is a per-chat default, never a constant in the code (SPEC 1.4, 5.5)."""
    item = achievement(rarity)
    assert passes_filters(item, chat(rarity_mode="rare"), threshold) is expected


def test_x360_passes_rare_mode_regardless_of_rarity() -> None:
    """Rarity is unknown for Xbox 360, not "too common" — a platform with no
    rarity data at all is exempt from the rarity check, not hidden by it
    (SPEC 5.5, 1.4 — one rarity_mode for every platform since M-Steam-2e,
    no more separate show_x360 switch)."""
    item = achievement(rarity=None, platform="x360")
    assert passes_filters(item, chat(rarity_mode="rare"), 10.0) is True


def test_hidden_mode_hides_every_platform_including_x360() -> None:
    """One switch for every platform (SPEC 9, M-Steam-2e) — 'hidden' now
    silences Xbox 360 too, not just the modern feed."""
    assert passes_filters(achievement(rarity=30.0), chat(rarity_mode="hidden"), 10.0) is False
    item = achievement(rarity=None, platform="x360")
    assert passes_filters(item, chat(rarity_mode="hidden"), 10.0) is False


def test_steam_is_not_exempt_from_the_rarity_check() -> None:
    """Unlike Xbox 360, Steam has a real rarity_percent (M-Steam-2b) — it
    goes through the ordinary rarity check, no platform exemption."""
    item = achievement(rarity=30.0, platform="steam")
    assert passes_filters(item, chat(rarity_mode="rare"), 10.0) is False  # 30% > 10%
    assert passes_filters(item, chat(rarity_mode="rare"), 50.0) is True


def test_rarity_mode_is_per_chat_now() -> None:
    """SPEC 9, M-Steam-2e's follow-up: rarity_mode moved off the person
    (one shared value for every chat) onto the subscription (one value per
    chat) — the same achievement can pass in one chat and not in another."""
    item = achievement(rarity=30.0)
    assert passes_filters(item, chat(rarity_mode="all"), 10.0) is True
    assert passes_filters(item, chat(rarity_mode="rare"), 10.0) is False  # 30% > 10%


def test_min_gamerscore_and_mute() -> None:
    assert passes_filters(achievement(gamerscore=5), chat(min_gamerscore=10), 10.0) is False
    assert passes_filters(achievement(), chat(muted=["1"]), 10.0) is False


def test_single_message_mentions_rarity_and_badge() -> None:
    text = format_single("Igor", achievement(rarity=2.4), "Halo Infinite")
    assert "Igor выбил «Ashes to Ashes»" in text
    assert "Halo Infinite · 🟢 Xbox · 20 G · редкость 2.4% 💎" in text


def test_single_message_for_x360_says_so_instead_of_rarity() -> None:
    text = format_single("Igor", achievement(rarity=None, platform="x360"), "Halo 3")
    assert "Halo 3 · 🟢 Xbox 360 · 20 G" in text
    assert "редкость" not in text


def test_single_message_omits_gamerscore_for_steam() -> None:
    """Steam has no gamerscore at all — always parsed as 0
    (services/steam/achievements.py) — "0 G" would read as a real score,
    not "not applicable" (SPEC 9, M-Steam-2e)."""
    item = achievement(rarity=92.2, platform="steam", gamerscore=0)
    text = format_single("Igor", item, "Deadlock")
    assert "G" not in text.split("\n")[1]


def test_single_message_tags_the_platform() -> None:
    """SPEC 9, M-Steam-2e — found live: a Steam achievement with no platform
    mention at all was easy to miss among Xbox ones."""
    text = format_single("Igor", achievement(rarity=92.2, platform="steam"), "Deadlock")
    assert "⚫ Steam" in text


def test_digest_counts_and_trims() -> None:
    items = [achievement(rarity=r) for r in (2.4, 11.0, 34.0, 50.0, 60.0)]
    text = format_digest("Igor", "Halo Infinite", items)
    assert "5 ачивок за сессию (+100 G)" in text
    assert "… и ещё 2" in text


def test_secret_achievement_name_and_description_are_spoilered() -> None:
    """Xbox's own isSecret does not redact name/description (found live) —
    hiding them is the bot's own doing, via a Telegram spoiler (SPEC 5.5, 7.1)."""
    text = format_single("Igor", achievement(is_secret=True), "Halo Infinite")
    assert '<span class="tg-spoiler">Ashes to Ashes</span>' in text
    assert '<span class="tg-spoiler">Kill 100 enemies</span>' in text


def test_non_secret_achievement_has_no_spoiler_markup() -> None:
    text = format_single("Igor", achievement(is_secret=False), "Halo Infinite")
    assert "tg-spoiler" not in text


def test_secret_achievement_name_is_spoilered_in_a_digest_line_too() -> None:
    items = [achievement(is_secret=True), achievement(is_secret=False)]
    text = format_digest("Igor", "Halo Infinite", items)
    assert '<span class="tg-spoiler">Ashes to Ashes</span>' in text
    assert "· Ashes to Ashes ·" in text  # the non-secret one, unwrapped


def test_gamertag_and_achievement_text_are_html_escaped() -> None:
    """format_single/format_digest go out as HTML now (for the spoiler
    markup) — untrusted text needs escaping or a stray "<"/"&" breaks
    Telegram's parser, same reasoning as the daily summary's table."""
    weird = achievement()
    weird.name = "A&B<C>"
    text = format_single("We>ird<Name", weird, "Hal&o")
    assert "<C>" not in text
    assert "&amp;" in text and "&lt;" in text


def test_platform_note_keys_on_platform_not_missing_rarity() -> None:
    """Backfilled rows come from contract 2, which carries no rarity — a modern
    game must not be labelled "Xbox 360" because of that."""
    assert platform_note("x360", None) == " · Xbox 360"
    assert platform_note("modern", 2.4) == " · 2.4%"
    assert platform_note("modern", None) == ""
