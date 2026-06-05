"""Single-instance lock — refuse to start a second bot on the same DB.

The audit caught two memebot processes running at once, both writing `observations` / `equity` /
per-mint creator tallies to the same SQLite — silently DOUBLE-COUNTING the dataset every downstream
decision depends on. This is an OS-level advisory lock on a `<db>.lock` file held for the process
lifetime: the OS releases it automatically on exit OR crash (no stale-pidfile problem). Two bots on
DIFFERENT DBs still run fine; two on the SAME DB: the second refuses to start.
"""
from __future__ import annotations

import os
import sys


class SingleInstanceError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        """Take an exclusive, non-blocking lock. Raises SingleInstanceError if another process
        (or another InstanceLock in this process) already holds it."""
        fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)   # non-blocking lock, 1 byte at offset 0
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise SingleInstanceError(
                f"another memebot already holds the lock {self.path} — refusing to start a second "
                "instance (two bots on one DB double-write the dataset). Stop the other first."
            ) from e
        self._fh = fh
        # record our PID for diagnostics (offset 1, NOT the locked byte 0)
        try:
            fh.seek(1)
            fh.truncate(1)
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        finally:
            self._fh = None
