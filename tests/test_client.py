"""Contract selection in the achievements request (SPEC 4, 5.3)."""

from __future__ import annotations

from typing import Any

from bot.services.xbox.client import XboxClient

MODERN_PAYLOAD = {
    "achievements": [
        {
            "id": "1",
            "name": "Not started yet",
            "progressState": "NotStarted",
            "rewards": [{"type": "Gamerscore", "value": "10"}],
            "titleAssociations": [{"name": "Modern Game", "id": 111}],
        },
        {
            "id": "2",
            "name": "Done",
            "progressState": "Achieved",
            "progression": {"timeUnlocked": "2026-01-01T00:00:00.0000000Z"},
            "rewards": [{"type": "Gamerscore", "value": "10"}],
            "rarity": {"currentPercentage": 12.5},
            "titleAssociations": [{"name": "Modern Game", "id": 111}],
        },
    ]
}

X360_PAYLOAD = {
    "achievements": [
        {
            "id": 7,
            "titleId": 222,
            "name": "Old school",
            "gamerscore": 25,
            "unlocked": True,
            "timeUnlocked": "2026-01-01T00:00:00.0000000Z",
        }
    ]
}


class StubResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload


class StubSession:
    def __init__(self, by_contract: dict[str, dict[str, Any]]) -> None:
        self._by_contract = by_contract
        self.contracts: list[str] = []

    async def get(self, url: str, params=None, headers=None) -> StubResponse:
        contract = (headers or {})["x-xbl-contract-version"]
        self.contracts.append(contract)
        return StubResponse(self._by_contract.get(contract, {"achievements": []}))


class StubXsts:
    xuid = "1"
    authorization_header_value = "XBL3.0 x=1;token"


class StubManager:
    def __init__(self, session: StubSession) -> None:
        self.session = session
        self.xsts_token = StubXsts()


class StubAuth:
    def __init__(self, manager: StubManager) -> None:
        self._manager = manager

    async def authenticated_manager(self, tg_id: int) -> StubManager:
        return self._manager


def _client(session: StubSession) -> XboxClient:
    return XboxClient(StubAuth(StubManager(session)))  # type: ignore[arg-type]


async def test_modern_title_uses_contract_4_only() -> None:
    session = StubSession({"4": MODERN_PAYLOAD})
    parsed = await _client(session).title_achievements(1, "111", "modern")

    assert session.contracts == ["4"]
    assert [item.achievement_id for item in parsed] == ["2"]
    assert parsed[0].rarity_percent == 12.5


async def test_back_compat_title_falls_back_to_contract_1() -> None:
    """Presence reports the console, not the game. A 360 title played on a
    Series X arrives as "modern" and contract 4 answers with an empty list —
    without the retry the whole session would publish nothing."""
    session = StubSession({"4": {"achievements": []}, "1": X360_PAYLOAD})

    parsed = await _client(session).title_achievements(1, "222", "modern")

    assert session.contracts == ["4", "1"]
    assert len(parsed) == 1
    assert parsed[0].platform == "x360"
    assert parsed[0].rarity_percent is None
    assert parsed[0].gamerscore == 25


async def test_known_x360_console_skips_contract_4() -> None:
    session = StubSession({"1": X360_PAYLOAD})
    parsed = await _client(session).title_achievements(1, "222", "x360")

    assert session.contracts == ["1"]
    assert parsed[0].platform == "x360"
