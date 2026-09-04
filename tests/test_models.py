"""Parsing of the three achievement payloads (SPEC 4, 5.3)."""

from __future__ import annotations

from bot.services.xbox.models import parse_achievements, parse_timestamp

MODERN = {
    "achievements": [
        {
            "id": "4",
            "name": "Prison Breakout",
            "description": "Completed tutorial path",
            "progressState": "Achieved",
            "progression": {"timeUnlocked": "2025-08-30T09:17:58.7770000Z"},
            "mediaAssets": [{"type": "Icon", "url": "https://example/icon.png"}],
            "rewards": [{"type": "Gamerscore", "value": "10"}],
            "rarity": {"currentCategory": "Common", "currentPercentage": 65.41},
            "titleAssociations": [{"name": "Gears of War: Reloaded", "id": 1829869520}],
            "isSecret": True,
        },
        {
            "id": "5",
            "name": "Still going",
            "progressState": "InProgress",
            "progression": {"timeUnlocked": "0001-01-01T00:00:00.0000000Z"},
            "rewards": [{"type": "Gamerscore", "value": "20"}],
            "rarity": {"currentPercentage": 3.0},
            "titleAssociations": [{"name": "Gears of War: Reloaded", "id": 1829869520}],
        },
    ]
}

X360 = {
    "achievements": [
        {
            "id": 62,
            "titleId": 1297287339,
            "name": "Unarmed and Dangerous",
            "description": "Killed 10 Locusts.",
            "gamerscore": 15,
            "unlocked": True,
            "timeUnlocked": "2026-08-31T16:17:05.8130000Z",
        },
        {"id": 63, "titleId": 1297287339, "name": "Locked one", "unlocked": False},
    ]
}


def test_modern_parses_rarity_and_icon() -> None:
    parsed = parse_achievements(MODERN, "modern")
    assert len(parsed) == 1  # InProgress is dropped
    item = parsed[0]
    assert item.rarity_percent == 65.41
    assert item.gamerscore == 10
    assert item.icon_url == "https://example/icon.png"
    assert item.title_id == "1829869520"
    assert item.title_name == "Gears of War: Reloaded"
    assert item.platform == "modern"
    assert item.is_secret is True


def test_missing_is_secret_defaults_to_false() -> None:
    """Most achievements have no isSecret field at all — absence must not be
    mistaken for "yes, secret"."""
    payload = {
        "achievements": [
            {
                "id": "9",
                "name": "Ordinary",
                "progressState": "Achieved",
                "progression": {"timeUnlocked": "2025-01-01T00:00:00.0000000Z"},
                "rewards": [{"type": "Gamerscore", "value": "5"}],
                "titleAssociations": [{"name": "Some Game", "id": 1}],
            }
        ]
    }
    assert parse_achievements(payload, "modern")[0].is_secret is False


def test_x360_is_never_secret() -> None:
    """Contract 1 has no isSecret concept at all — X360Achievement.to_parsed()
    always defaults it to False."""
    assert parse_achievements(X360, "x360", "1297287339")[0].is_secret is False


def test_in_progress_never_becomes_a_row() -> None:
    """An InProgress row in seen_achievements would hide the achievement
    forever, because the real unlock would look like a duplicate (SPEC 5.3)."""
    assert [item.achievement_id for item in parse_achievements(MODERN, "modern")] == ["4"]


def test_missing_rarity_block_does_not_crash() -> None:
    payload = {
        "achievements": [
            {
                "id": "9",
                "name": "No rarity here",
                "progressState": "Achieved",
                "progression": {"timeUnlocked": "2025-01-01T00:00:00.0000000Z"},
                "rewards": [{"type": "Gamerscore", "value": "5"}],
                "titleAssociations": [{"name": "Some Game", "id": 1}],
            }
        ]
    }
    parsed = parse_achievements(payload, "modern")
    assert len(parsed) == 1
    assert parsed[0].rarity_percent is None


def test_broken_record_is_skipped_not_fatal() -> None:
    payload = {"achievements": [{"nonsense": True}, MODERN["achievements"][0]]}
    assert len(parse_achievements(payload, "modern")) == 1


def test_x360_has_no_rarity_and_only_unlocked() -> None:
    parsed = parse_achievements(X360, "x360", "1297287339")
    assert len(parsed) == 1
    assert parsed[0].rarity_percent is None
    assert parsed[0].platform == "x360"
    assert parsed[0].gamerscore == 15


def test_seven_digit_fraction_and_placeholder_dates() -> None:
    assert parse_timestamp("2025-08-30T09:17:58.7770000Z") is not None
    assert parse_timestamp("0001-01-01T00:00:00.0000000Z") is None
    # The other Microsoft placeholder; counting it would put unlocks in 1753.
    assert parse_timestamp("1753-01-01T00:00:00.0000000Z") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("not a date") is None
