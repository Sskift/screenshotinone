"""Durable upload receipts and a cross-process lock for a state database."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
from typing import Iterator


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def exclusive_state_lock(path: Path) -> Iterator[None]:
    """OS locks are released even when a worker crashes; no stale PID recovery."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise AlreadyRunningError("Another sync is using this state database.") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class State:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.connection: sqlite3.Connection | None = None
        if readonly:
            if not path.exists():
                return
            self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, isolation_level=None)
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS uploads ("
                "target TEXT NOT NULL, source_key TEXT NOT NULL, digest TEXT NOT NULL, "
                "receipt TEXT NOT NULL, uploaded_at TEXT NOT NULL, "
                "PRIMARY KEY (target, source_key, digest))"
            )

    def contains(self, target: str, source_key: str, digest: str) -> bool:
        if self.connection is None:
            return False
        return self.connection.execute(
            "SELECT 1 FROM uploads WHERE target = ? AND source_key = ? AND digest = ?",
            (target, source_key, digest),
        ).fetchone() is not None

    def record(self, target: str, source_key: str, digest: str, receipt: str) -> None:
        if self.connection is None:
            raise RuntimeError("Cannot write absent read-only state")
        # Autocommit makes each successful receipt durable before the next upload.
        self.connection.execute(
            "INSERT OR REPLACE INTO uploads VALUES (?, ?, ?, ?, ?)",
            (target, source_key, digest, receipt, datetime.now(timezone.utc).isoformat()),
        )

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
