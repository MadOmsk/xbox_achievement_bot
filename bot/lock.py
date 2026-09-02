"""Single-instance lock.

Two bots on one token fight over Telegram updates: each getUpdates call steals
the queue from the other, so both drop messages and the log fills with 409s.
The database gets a second writer as well.

The check lives in the process, not in the launcher, because starting the bot
by hand (`python -m bot.main`) must be just as safe as `manage.ps1 start`.
The lock is held by the operating system and disappears when the process dies —
a stale file after a crash unlocks itself, unlike a PID file.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

if sys.platform == "win32":
    import msvcrt

    _fcntl: ModuleType | None = None
else:
    import fcntl as _fcntl_module

    _fcntl = _fcntl_module
    msvcrt = None  # type: ignore[assignment]


class AlreadyRunningError(RuntimeError):
    """Another copy of the bot holds the lock."""


@contextmanager
def single_instance(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        _acquire(handle)
    except OSError:
        handle.close()
        raise AlreadyRunningError(str(path)) from None

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            _release(handle)
        finally:
            handle.close()


def _acquire(handle) -> None:
    handle.seek(0)
    if msvcrt is not None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        assert _fcntl is not None
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)


def _release(handle) -> None:
    try:
        handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            assert _fcntl is not None
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
    except OSError:
        # Releasing is best effort: the lock dies with the process anyway.
        pass
