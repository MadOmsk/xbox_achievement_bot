"""/online's auto-refresh (Follow-up 2026-09-05) — created_at drives the
3h cutoff, last_updated_at drives the 10-minute refresh clock, one row per
chat (a fresh /online supersedes whatever was auto-refreshing before)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.db.repo import Repo
from bot.poller.online_refresh import OnlineAutoRefresh, refresh_interval_minutes, ttl_hours

CHAT_ID = -100777
XUID = "xuid-a"


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.edits: list[tuple[int, int, str]] = []
        self._fail = fail

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, **kwargs: object
    ) -> None:
        if self._fail:
            raise RuntimeError("message not found")
        self.edits.append((chat_id, message_id, text))


async def _backdate(repo: Repo, *, created_minutes_ago: int, updated_minutes_ago: int) -> None:
    now = datetime.now(UTC)
    created = (now - timedelta(minutes=created_minutes_ago)).isoformat(timespec="seconds")
    updated = (now - timedelta(minutes=updated_minutes_ago)).isoformat(timespec="seconds")
    await repo._conn.execute(
        "UPDATE online_auto_refresh SET created_at = ?, last_updated_at = ? WHERE chat_id = ?",
        (created, updated, CHAT_ID),
    )
    await repo._conn.commit()


async def _member(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    await repo.ensure_user(1, "igor")
    await repo.link_xbox_account(1, XUID, "Igor", 0)
    await repo.subscribe(CHAT_ID, 1)


async def test_defaults(repo: Repo) -> None:
    assert await refresh_interval_minutes(repo) == 10
    assert await ttl_hours(repo) == 3


async def test_start_supersedes_a_previous_auto_refresh(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    await repo.start_online_auto_refresh(CHAT_ID, 1)
    await repo.start_online_auto_refresh(CHAT_ID, 2)

    rows = await repo.all_online_auto_refreshes()

    assert len(rows) == 1
    assert rows[0].message_id == 2


async def test_tick_refreshes_a_message_past_its_interval(repo: Repo) -> None:
    await _member(repo)
    await repo.start_online_auto_refresh(CHAT_ID, 42)
    await _backdate(repo, created_minutes_ago=30, updated_minutes_ago=11)

    bot = FakeBot()
    await OnlineAutoRefresh(bot, repo).tick()

    assert len(bot.edits) == 1
    chat_id, message_id, text = bot.edits[0]
    assert (chat_id, message_id) == (CHAT_ID, 42)
    assert "Igor" in text
    rows = await repo.all_online_auto_refreshes()
    assert rows[0].last_updated_at != rows[0].created_at


async def test_tick_leaves_a_fresh_message_alone(repo: Repo) -> None:
    await _member(repo)
    await repo.start_online_auto_refresh(CHAT_ID, 42)
    await _backdate(repo, created_minutes_ago=1, updated_minutes_ago=1)

    bot = FakeBot()
    await OnlineAutoRefresh(bot, repo).tick()

    assert bot.edits == []
    assert len(await repo.all_online_auto_refreshes()) == 1


async def test_tick_stops_past_the_ttl(repo: Repo) -> None:
    await _member(repo)
    await repo.start_online_auto_refresh(CHAT_ID, 42)
    await _backdate(repo, created_minutes_ago=200, updated_minutes_ago=11)  # > 3h

    bot = FakeBot()
    await OnlineAutoRefresh(bot, repo).tick()

    assert bot.edits == []
    assert await repo.all_online_auto_refreshes() == []


async def test_ttl_zero_expires_everything_immediately(repo: Repo) -> None:
    await _member(repo)
    await repo.start_online_auto_refresh(CHAT_ID, 42)
    await repo.set_app_setting("online_refresh_ttl_hours", "0", 1)

    await OnlineAutoRefresh(FakeBot(), repo).tick()

    assert await repo.all_online_auto_refreshes() == []


async def test_interval_zero_disables_refreshing_but_not_ttl(repo: Repo) -> None:
    await _member(repo)
    await repo.start_online_auto_refresh(CHAT_ID, 42)
    await _backdate(repo, created_minutes_ago=30, updated_minutes_ago=30)
    await repo.set_app_setting("online_refresh_interval_min", "0", 1)

    bot = FakeBot()
    await OnlineAutoRefresh(bot, repo).tick()

    assert bot.edits == []
    assert len(await repo.all_online_auto_refreshes()) == 1  # ttl (3h) not reached yet


async def test_a_failed_edit_stops_tracking_the_chat(repo: Repo) -> None:
    """The message was deleted, or the bot got kicked — nothing left worth
    retrying, same reasoning as message_cleanup.py's own error handling."""
    await _member(repo)
    await repo.start_online_auto_refresh(CHAT_ID, 42)
    await _backdate(repo, created_minutes_ago=30, updated_minutes_ago=11)

    await OnlineAutoRefresh(FakeBot(fail=True), repo).tick()

    assert await repo.all_online_auto_refreshes() == []


async def test_no_members_left_stops_tracking_the_chat(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    await repo.start_online_auto_refresh(CHAT_ID, 42)
    await _backdate(repo, created_minutes_ago=30, updated_minutes_ago=11)

    bot = FakeBot()
    await OnlineAutoRefresh(bot, repo).tick()

    assert bot.edits == []
    assert await repo.all_online_auto_refreshes() == []
