"""/recent as a readable list, not a code-block table (SPEC 6.3)."""

from __future__ import annotations

from bot.db.repo import RecentAchievement
from bot.handlers.chat import _recent_list, _recent_row


def row(
    gamertag: str | None = "Igor",
    name: str = "Ashes to Ashes",
    game: str | None = "Halo Infinite",
    gamerscore: int = 50,
    rarity_percent: float | None = 2.4,
    is_secret: bool = False,
) -> RecentAchievement:
    return RecentAchievement(
        gamertag=gamertag,
        name=name,
        game=game,
        gamerscore=gamerscore,
        rarity_percent=rarity_percent,
        platform="modern",
        unlocked_at="2026-09-02T10:00:00+00:00",
        is_secret=is_secret,
    )


def test_recent_row_mentions_rarity_badge() -> None:
    """The badge leads the line now, not a fixed generic bullet (2026-09-05)
    — it always shows one of the two icons, never both a bullet and a badge
    on the same "common" row."""
    assert _recent_row(row(rarity_percent=2.4)).startswith("💎 ")
    assert _recent_row(row(rarity_percent=None)).startswith("🏆 ")


def test_recent_row_gamerscore_is_in_parentheses() -> None:
    assert "(+50 G)" in _recent_row(row(gamerscore=50))


def test_recent_row_long_names_are_truncated() -> None:
    long_name = "A" * 40
    line = _recent_row(row(gamertag=long_name, name=long_name, game=long_name))
    assert "A" * 40 not in line
    assert "…" in line


def test_secret_achievement_name_is_a_real_spoiler_not_a_placeholder() -> None:
    """A blockquote (unlike the old <pre> table) can host a real Telegram
    spoiler — the actual name stays in the message, just hidden (SPEC 7.1)."""
    line = _recent_row(row(name="Ashes to Ashes", is_secret=True))
    assert '<span class="tg-spoiler">Ashes to Ashes</span>' in line


def test_non_secret_row_has_no_spoiler_markup() -> None:
    line = _recent_row(row(name="Ashes to Ashes", is_secret=False))
    assert "tg-spoiler" not in line
    assert "Ashes to Ashes" in line


def test_recent_row_text_is_html_escaped() -> None:
    line = _recent_row(row(gamertag="We>ird<Name", name="A&B<C>", game="Hal&o"))
    assert "<C>" not in line
    assert "&amp;" in line and "&lt;" in line


def test_recent_list_is_a_collapsible_blockquote() -> None:
    text = _recent_list([row(), row(gamertag="Alex", rarity_percent=None)])
    assert text.startswith("<blockquote expandable>")
    assert text.endswith("</blockquote>")
    assert "Igor" in text and "Alex" in text
