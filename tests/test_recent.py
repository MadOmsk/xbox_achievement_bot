"""/recent as a table, not a free-text feed (SPEC 6.3)."""

from __future__ import annotations

from bot.db.repo import RecentAchievement
from bot.handlers.chat import SECRET_ACHIEVEMENT_NAME, _recent_row, _recent_table


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


def test_recent_row_prefixes_the_achievement_with_a_rarity_badge() -> None:
    cells = _recent_row(row(rarity_percent=2.4))
    assert cells[1].startswith("💎")

    cells = _recent_row(row(rarity_percent=None))
    assert not cells[1].startswith(("💎", "⭐"))


def test_recent_row_long_names_are_truncated() -> None:
    long_name = "A" * 40
    cells = _recent_row(row(gamertag=long_name, name=long_name, game=long_name))
    assert all(len(cell) <= 18 for cell in cells[:3])
    assert all(cell.endswith("…") for cell in cells[:3])


def test_recent_table_renders_a_monospace_block() -> None:
    text = _recent_table([row(), row(gamertag="Alex", rarity_percent=None)])
    assert text.startswith("<pre>") and text.endswith("</pre>")
    assert "Игрок" in text and "Ачивка" in text and "Когда" in text


def test_secret_achievement_name_is_replaced_not_shown() -> None:
    """A real Telegram spoiler can't live inside a <pre> table, so /recent
    hides a secret achievement's name with a plain placeholder instead
    (SPEC 6.3, 7.1) — everything else about the row is unaffected."""
    cells = _recent_row(row(name="Ashes to Ashes", is_secret=True))
    assert "Ashes to Ashes" not in cells[1]
    assert SECRET_ACHIEVEMENT_NAME in cells[1]


def test_non_secret_row_shows_the_real_name() -> None:
    cells = _recent_row(row(name="Ashes to Ashes", is_secret=False))
    assert "Ashes to Ashes" in cells[1]
