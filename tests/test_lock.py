"""Only one bot at a time (bot/lock.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.lock import AlreadyRunningError, single_instance


def test_second_holder_is_refused(tmp_path: Path) -> None:
    """Two bots on one token steal each other's Telegram updates."""
    lock = tmp_path / "bot.lock"
    with single_instance(lock), pytest.raises(AlreadyRunningError):
        with single_instance(lock):
            pass


def test_lock_is_released_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "bot.lock"
    with single_instance(lock):
        pass
    # A crashed run must not leave the bot unstartable, so the file staying
    # behind is fine as long as the lock itself is gone.
    with single_instance(lock):
        pass


def test_pid_is_written(tmp_path: Path) -> None:
    import os

    lock = tmp_path / "bot.lock"
    with single_instance(lock):
        assert lock.exists()
    assert lock.read_text().strip() == str(os.getpid())
