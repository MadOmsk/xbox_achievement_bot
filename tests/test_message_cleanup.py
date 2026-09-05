"""Auto-delete for "system" group messages (2026-09-05 follow-up, SPEC 9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.db.repo import Repo
from bot.poller.message_cleanup import MessageCleanup, system_message_ttl_minutes

CHAT_ID = -100777


class FakeBot:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.deleted: list[tuple[int, int]] = []
        self._fail_on = fail_on or set()

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        if message_id in self._fail_on:
            raise RuntimeError("too old to delete")
        self.deleted.append((chat_id, message_id))


async def _log_old(repo: Repo, message_id: int, *, is_system: bool, minutes_ago: int) -> None:
    await repo.log_bot_message(CHAT_ID, message_id, is_system=is_system)
    # Backdate sent_at directly — log_bot_message always stamps "now".
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    await repo._conn.execute(
        "UPDATE bot_messages SET sent_at = ? WHERE chat_id = ? AND message_id = ?",
        (stamp, CHAT_ID, message_id),
    )
    await repo._conn.commit()


async def test_default_ttl_is_five_minutes(repo: Repo) -> None:
    assert await system_message_ttl_minutes(repo) == 5


async def test_ttl_zero_disables_cleanup_entirely(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    await repo.set_app_setting("system_message_ttl_min", "0", 1)
    await _log_old(repo, 1, is_system=True, minutes_ago=60)

    await MessageCleanup(FakeBot(), repo).tick()

    assert await repo.all_system_bot_messages(CHAT_ID) == [1]


async def test_deletes_only_system_messages_past_the_ttl(repo: Repo) -> None:
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    await _log_old(repo, 1, is_system=True, minutes_ago=10)  # past the 5-minute default
    await _log_old(repo, 2, is_system=True, minutes_ago=1)  # too fresh
    await _log_old(repo, 3, is_system=False, minutes_ago=10)  # a "stats" result, never touched

    bot = FakeBot()
    await MessageCleanup(bot, repo).tick()

    assert bot.deleted == [(CHAT_ID, 1)]
    remaining = await repo.bot_messages_since(CHAT_ID, datetime.now(UTC) - timedelta(hours=1))
    assert sorted(remaining) == [2, 3]


async def test_a_failed_delete_still_forgets_the_row(repo: Repo) -> None:
    """Same principle as /delete_last and the admin panel's own bulk wipe —
    too old (Telegram's 48h cap) or already gone either way, nothing left
    worth retrying."""
    await repo.upsert_chat(CHAT_ID, "Test chat", 1)
    await _log_old(repo, 1, is_system=True, minutes_ago=10)

    await MessageCleanup(FakeBot(fail_on={1}), repo).tick()

    assert await repo.bot_messages_since(CHAT_ID, datetime.now(UTC) - timedelta(hours=1)) == []
