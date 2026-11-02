"""Single-pass synchronization; the CLI schedules repeated passes."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Iterable, Protocol

from .source import Screenshot, scan_screenshots
from .state import State, exclusive_state_lock


class Target(Protocol):
    identity: str

    def upload(self, screenshot: Screenshot, snapshot_path: Path, digest: str) -> str: ...


@dataclass
class SyncReport:
    scanned: int = 0
    uploaded: int = 0
    skipped: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)


class SourceChangedError(OSError):
    """A screenshot is still being saved; retry it in a later pass."""


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    # Windows Python 3.12 can report creation time from lstat() but change time
    # from fstat() as st_ctime, so comparing those produces false instability.
    change_time = 0 if os.name == "nt" else info.st_ctime_ns
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change_time


def _snapshot(source: Path, destination: Path, settle_seconds: float) -> str:
    before = source.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or getattr(before, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or before.st_size == 0
        or time.time() - before.st_mtime < settle_seconds
    ):
        raise SourceChangedError("Screenshot is not yet stable")
    digest = hashlib.sha256()
    with source.open("rb") as original, destination.open("wb") as snapshot:
        if _signature(os.fstat(original.fileno())) != _signature(before):
            raise SourceChangedError("Screenshot changed before reading")
        while block := original.read(1024 * 1024):
            snapshot.write(block)
            digest.update(block)
        after_read = os.fstat(original.fileno())
    after = source.lstat()
    if (
        _signature(before) != _signature(after_read)
        or _signature(before) != _signature(after)
        or destination.stat().st_size != before.st_size
    ):
        raise SourceChangedError("Screenshot changed while reading")
    return digest.hexdigest()


def sync_once(
    roots: Iterable[Path],
    target: Target,
    state_path: Path,
    *,
    settle_seconds: float = 3.0,
    dry_run: bool = False,
) -> SyncReport:
    """Upload stable originals once per target, source path, and SHA-256.

    Dry runs read existing state but create no database, lock, destination or
    uploads. Temporary snapshots are always removed. Target implementations
    should use the digest for idempotency if the process dies after an upload
    succeeds but before its local receipt can be recorded.
    """
    if not math.isfinite(settle_seconds) or settle_seconds < 0:
        raise ValueError("settle_seconds must be a finite nonnegative number")
    if not target.identity:
        raise ValueError("target.identity must not be empty")
    report = SyncReport()
    screenshots: list[Screenshot] = []
    seen: set[str] = set()
    def on_scan_error(path: Path, error: OSError) -> None:
        report.errors.append(f"Cannot scan {path}: {error}")

    for screenshot in scan_screenshots(roots, on_error=on_scan_error):
        key = os.path.normcase(str(screenshot.path))
        if key not in seen:
            seen.add(key)
            screenshots.append(screenshot)
    report.scanned = len(screenshots)
    state_path = Path(state_path).expanduser().resolve()
    lock = nullcontext() if dry_run else exclusive_state_lock(state_path)
    with lock, State(state_path, readonly=dry_run) as state:
        for screenshot in screenshots:
            try:
                with tempfile.TemporaryDirectory(prefix="screenshotinone-") as temporary:
                    snapshot = Path(temporary) / ("snapshot" + screenshot.path.suffix)
                    try:
                        digest = _snapshot(screenshot.path, snapshot, settle_seconds)
                    except (SourceChangedError, FileNotFoundError):
                        report.pending += 1
                        continue
                    if state.contains(target.identity, screenshot.relative_path, digest):
                        report.skipped += 1
                    elif dry_run:
                        report.planned.append(screenshot.relative_path)
                    else:
                        receipt = target.upload(screenshot, snapshot, digest)
                        state.record(target.identity, screenshot.relative_path, digest, receipt)
                        report.uploaded += 1
            except Exception as error:
                report.errors.append(f"{screenshot.relative_path}: {error}")
    return report
