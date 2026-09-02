"""Operator notifications (SPEC 6.5)."""

from __future__ import annotations

from bot.db.repo import Repo
from bot.services.notify import AdminNotifier

TG_ID = 42


class FakeBot:
    def __init__(self, failing: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self._failing = failing or set()

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        if chat_id in self._failing:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


async def test_every_admin_is_told(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "igor")
    bot = FakeBot()
    notifier = AdminNotifier(bot, repo, [1, 2])  # type: ignore[arg-type]

    await notifier.user_connected(TG_ID, "Mad Omsk", is_new=True)

    assert [chat_id for chat_id, _ in bot.sent] == [1, 2]
    assert "Добавлен пользователь: Mad Omsk" in bot.sent[0][1]
    assert "@igor" in bot.sent[0][1]


async def test_reconnect_is_worded_differently(repo: Repo) -> None:
    await repo.ensure_user(TG_ID)
    bot = FakeBot()
    notifier = AdminNotifier(bot, repo, [1])  # type: ignore[arg-type]

    await notifier.user_connected(TG_ID, "Mad Omsk", is_new=False)

    assert "Переподключился" in bot.sent[0][1]
    assert "без username" in bot.sent[0][1]


async def test_one_blocked_admin_does_not_stop_the_rest(repo: Repo) -> None:
    """The notification is a side effect; it must not break what triggered it."""
    await repo.ensure_user(TG_ID)
    bot = FakeBot(failing={1})
    notifier = AdminNotifier(bot, repo, [1, 2])  # type: ignore[arg-type]

    await notifier.token_dead(TG_ID)

    assert [chat_id for chat_id, _ in bot.sent] == [2]
    assert "слетел вход" in bot.sent[0][1]
