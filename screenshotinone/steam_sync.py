"""An opt-in, durable queue for Steam's native screenshot publisher.

The queue is deliberately independent of cloud-folder synchronization. A native
upload with an unknown outcome is reconciled, never automatically submitted a
second time. All native calls are supplied by a bridge owned by the caller.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Iterable, Protocol

from .engine import SourceChangedError, _snapshot
from .source import Screenshot, scan_screenshots
from .state import exclusive_state_lock


VISIBILITIES = frozenset({"private", "friends", "public", "unlisted"})
MAX_ATTEMPTS = 3


class SteamBridgeProtocol(Protocol):
    def doctor(self) -> dict[str, Any]: ...
    def list_screenshots(self) -> list[Any]: ...
    def get_screenshot(self, game_id: str, handle: int) -> Any: ...
    def publish(self, shot: Any, visibility: str = "private") -> bool: ...


@dataclass
class SteamSyncReport:
    scanned: int = 0
    baseline: int = 0
    queued: int = 0
    uploaded: int = 0
    skipped: int = 0
    pending: int = 0
    uncertain: int = 0
    errors: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _scope_key(roots: list[Path], account_id: str) -> str:
    roots_digest = hashlib.sha256("\n".join(sorted({_path_key(p) for p in roots})).encode()).hexdigest()
    return f"baseline:{account_id}:{roots_digest}"


def _stable_digest(path: Path, settle_seconds: float) -> str:
    with tempfile.TemporaryDirectory(prefix="screenshotinone-steam-") as directory:
        return _snapshot(path, Path(directory) / "snapshot", settle_seconds)


def _ugc_id(value: Any) -> str | None:
    # The bridge preserves uint64 IDs as strings; floats, bools and JS numbers
    # can lose precision and must never become a purported successful upload.
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 20:
        return None
    number = int(value)
    return str(number) if 0 < number < (1 << 64) - 1 else None


def _permalink(account_id: str, published_file_id: str | None) -> str:
    if published_file_id is not None:
        return f"https://steamcommunity.com/sharedfiles/filedetails/?id={published_file_id}"
    # ugcHandle is a UFS content-storage handle, not the published file ID.
    # A profile gallery is a valid fallback without inventing a per-image URL.
    steam_id64 = 76561197960265728 + int(account_id)
    return f"https://steamcommunity.com/profiles/{steam_id64}/screenshots/"


class _Queue:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.connection: sqlite3.Connection | None = None
        if readonly:
            if not path.exists():
                return
            self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        if not readonly:
            self.connection.executescript(
                "CREATE TABLE IF NOT EXISTS steam_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS steam_queue ("
                "account_id TEXT NOT NULL, source_key TEXT NOT NULL, path TEXT NOT NULL, "
                "game_id TEXT NOT NULL, relative_path TEXT NOT NULL, digest TEXT, "
                "status TEXT NOT NULL, visibility TEXT NOT NULL, native_handle INTEGER, "
                "ugc_id TEXT, published_file_id TEXT, permalink TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
                "next_attempt REAL NOT NULL DEFAULT 0, error TEXT, updated_at REAL NOT NULL, "
                "PRIMARY KEY(account_id, source_key));"
            )
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(steam_queue)")}
            if "published_file_id" not in columns:
                self.connection.execute("ALTER TABLE steam_queue ADD COLUMN published_file_id TEXT")

    def baseline(self, scope: str) -> bool:
        return self.connection is not None and self.connection.execute(
            "SELECT 1 FROM steam_meta WHERE key = ?", (scope,)
        ).fetchone() is not None

    def establish_baseline(self, scope: str) -> None:
        assert self.connection is not None
        self.connection.execute(
            "INSERT OR IGNORE INTO steam_meta VALUES (?, ?)", (scope, str(time.time()))
        )

    def entries(self, account_id: str | None = None) -> list[dict[str, Any]]:
        if self.connection is None:
            return []
        query = "SELECT * FROM steam_queue"
        parameters: tuple[str, ...] = ()
        if account_id is not None:
            query += " WHERE account_id = ?"
            parameters = (account_id,)
        return [dict(row) for row in self.connection.execute(query + " ORDER BY source_key", parameters)]

    def insert(self, source: Screenshot, status: str, visibility: str) -> dict[str, Any]:
        assert self.connection is not None
        self.connection.execute(
            "INSERT INTO steam_queue (account_id, source_key, path, game_id, relative_path, "
            "status, visibility, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source.account_id, _path_key(source.path), str(source.path), source.app_id,
             source.relative_path, status, visibility, time.time()),
        )
        return dict(self.connection.execute(
            "SELECT * FROM steam_queue WHERE account_id = ? AND source_key = ?",
            (source.account_id, _path_key(source.path)),
        ).fetchone())

    def update(self, entry: dict[str, Any], **changes: Any) -> None:
        assert self.connection is not None
        allowed = {"digest", "status", "visibility", "native_handle", "ugc_id", "published_file_id", "permalink", "attempts", "next_attempt", "error"}
        if not set(changes) <= allowed:
            raise ValueError("Invalid queue update")
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        self.connection.execute(
            f"UPDATE steam_queue SET {assignments} WHERE account_id = ? AND source_key = ?",
            (*changes.values(), entry["account_id"], entry["source_key"]),
        )
        entry.update(changes)

    def __enter__(self) -> "_Queue":
        return self

    def __exit__(self, *_: Any) -> None:
        if self.connection is not None:
            self.connection.close()


def steam_queue_status(state_path: Path, account_id: str | None = None) -> dict[str, Any]:
    """Inspect local receipts and pending work without creating files."""
    path = Path(state_path).expanduser().resolve()
    with _Queue(path, readonly=True) as queue:
        entries = queue.entries(account_id)
        counts: dict[str, int] = {}
        for entry in entries:
            status = "uncertain" if entry["status"] == "publishing" else entry["status"]
            counts[status] = counts.get(status, 0) + 1
        established = False
        if queue.connection is not None:
            prefix = f"baseline:{account_id}:" if account_id is not None else "baseline:"
            established = any(
                row[0].startswith(prefix)
                for row in queue.connection.execute("SELECT key FROM steam_meta")
            )
        return {
            "counts": counts,
            "baseline_established": established,
            "entries": sorted(entries, key=lambda entry: entry["updated_at"], reverse=True)[:50],
            "errors": [entry["error"] for entry in entries if entry["error"]],
            "links": [entry["permalink"] for entry in entries if entry["status"] == "confirmed" and entry["permalink"]],
        }


def _validate_native(shot: Any, entry: dict[str, Any], *, handle: int | None = None) -> None:
    game_id = getattr(shot, "game_id", getattr(shot, "str_game_id", None))
    if getattr(shot, "account_id", None) != entry["account_id"]:
        raise ValueError("Steam screenshot belongs to a different account")
    if game_id != entry["game_id"] or _path_key(shot.path) != entry["source_key"]:
        raise ValueError("Steam screenshot path or game does not match the queued original")
    native_handle = getattr(shot, "handle", None)
    if type(native_handle) is not int or not 0 < native_handle <= 0xFFFFFFFF:
        raise ValueError("Steam screenshot has an invalid native handle")
    if handle is not None and native_handle != handle:
        raise ValueError("Steam screenshot handle changed")
    if type(getattr(shot, "uploaded", None)) is not bool:
        raise ValueError("Steam returned an invalid uploaded flag")


def _native_publication(shot: Any, visibility: str) -> tuple[str, str | None]:
    """Return unuploaded, confirmed, or unsafe; never infer success from IDs alone."""
    value = getattr(shot, "ugc_id", None)
    ugc_id = _ugc_id(value)
    if shot.uploaded is True:
        if ugc_id is None:
            return "unsafe", "Steam marks the screenshot uploaded but has no valid UGC ID"
        if getattr(shot, "visibility", None) != visibility:
            return "unsafe", "Existing Steam publication visibility does not match the requested visibility"
        return "confirmed", ugc_id
    if value is not None:
        return "unsafe", "Steam returned inconsistent publication metadata"
    return "unuploaded", None


def _error(report: SteamSyncReport, entry: dict[str, Any], message: str) -> None:
    report.errors.append(f"{entry['relative_path']}: {message}")


def _confirm(queue: _Queue, entry: dict[str, Any], shot: Any, ugc_id: str, report: SteamSyncReport) -> None:
    published_file_id = _ugc_id(getattr(shot, "published_file_id", None))
    link = _permalink(entry["account_id"], published_file_id)
    queue.update(entry, status="confirmed", ugc_id=ugc_id, published_file_id=published_file_id, permalink=link, error=None, next_attempt=0)
    report.uploaded += 1
    report.links.append(link)


def _dry_run(
    sources: list[Screenshot], queue: _Queue, scope: str, account_id: str,
    visibility: str, include_existing: bool, only_path: Path | None,
    settle_seconds: float, limit: int | None, report: SteamSyncReport,
) -> None:
    entries = {entry["source_key"]: entry for entry in queue.entries(account_id)}
    is_first = not queue.baseline(scope)
    for source in sources:
        entry = entries.get(_path_key(source.path))
        status = entry["status"] if entry else ("baseline" if is_first and not include_existing and only_path is None else "queued")
        if status == "baseline" and not include_existing and only_path is None:
            report.baseline += 1
            continue
        if status in {"publishing", "uncertain"}:
            report.uncertain += 1
            continue
        if status == "confirmed":
            report.skipped += 1
            if entry["visibility"] == visibility and entry["permalink"]:
                report.links.append(entry["permalink"])
            elif entry["visibility"] != visibility:
                _error(report, entry, "Recorded publication has a different requested visibility")
            continue
        if status in {"blocked", "failed"}:
            _error(report, entry, entry["error"] or "Publication is blocked")
            continue
        report.queued += 1
        try:
            digest = _stable_digest(source.path, settle_seconds)
            if entry and entry["digest"] and digest != entry["digest"]:
                _error(report, entry, "Original changed after it was queued")
            elif limit is None or len(report.planned) < limit:
                report.planned.append(source.relative_path)
        except (SourceChangedError, FileNotFoundError):
            report.pending += 1
        except OSError as error:
            report.errors.append(f"{source.relative_path}: {error}")


def steam_sync_once(
    roots: Iterable[Path],
    bridge: SteamBridgeProtocol,
    state_path: Path,
    *,
    account_id: str,
    visibility: str = "private",
    include_existing: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    settle_seconds: float = 3.0,
    confirm_timeout: float = 10.0,
    only_path: Path | None = None,
) -> SteamSyncReport:
    """Queue stable originals and conservatively confirm native publication.

    The first ordinary scan records a baseline without publishing historical
    images. ``include_existing`` explicitly queues that history. ``only_path``
    opts in exactly one file and never establishes the ordinary-scan baseline.
    Ambiguous submissions remain uncertain until native metadata proves their
    outcome; an unuploaded probe alone never clears uncertainty.
    """
    if not isinstance(account_id, str) or not account_id.isascii() or not account_id.isdecimal():
        raise ValueError("account_id must be a Steam userdata account ID")
    if len(account_id) > 10 or not 0 < int(account_id) <= 0xFFFFFFFF:
        raise ValueError("account_id must be a nonzero uint32 Steam userdata account ID")
    if visibility not in VISIBILITIES:
        raise ValueError("Unsupported Steam publication visibility")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    for name, number in (("settle_seconds", settle_seconds), ("confirm_timeout", confirm_timeout)):
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    root_list = [Path(root).expanduser().resolve() for root in roots]
    scope = _scope_key(root_list, account_id)
    report = SteamSyncReport()
    sources = scan_screenshots(
        root_list,
        on_error=lambda path, error: report.errors.append(f"Cannot scan {path}: {error}"),
    )
    scan_failed = bool(report.errors)
    sources = [source for source in sources if source.account_id == account_id]
    selected_key = _path_key(only_path) if only_path is not None else None
    if selected_key is not None:
        sources = [source for source in sources if _path_key(source.path) == selected_key]
        if not sources:
            report.errors.append("Selected file is not an original screenshot for the pinned account and Steam roots")
            return report
    report.scanned = len(sources)
    source_map = {_path_key(source.path): source for source in sources}
    path = Path(state_path).expanduser().resolve()
    lock = nullcontext() if dry_run else exclusive_state_lock(path)
    with lock, _Queue(path, readonly=dry_run) as queue:
        if dry_run:
            _dry_run(sources, queue, scope, account_id, visibility, include_existing, only_path, settle_seconds, limit, report)
            return report
        known = {entry["source_key"]: entry for entry in queue.entries(account_id)}
        first_scan = not queue.baseline(scope)
        work: list[dict[str, Any]] = []
        # Everything discovered is persisted before the first bridge call.
        for source in sources:
            key = _path_key(source.path)
            entry = known.get(key)
            if entry is None:
                status = "baseline" if first_scan and not include_existing and only_path is None else "queued"
                entry = queue.insert(source, status, visibility)
                known[key] = entry
            if entry["status"] == "baseline":
                if include_existing or only_path is not None:
                    queue.update(entry, status="queued", visibility=visibility, error=None)
                else:
                    report.baseline += 1
                    continue
            if entry["status"] == "confirmed":
                report.skipped += 1
                if entry["visibility"] != visibility:
                    _error(report, entry, "Recorded publication has a different requested visibility")
                    continue
                try:
                    if _stable_digest(source.path, settle_seconds) != entry["digest"]:
                        message = "Original changed since confirmed publication; the existing Steam upload is preserved"
                        queue.update(entry, error=message)
                        _error(report, entry, message)
                    elif entry["permalink"]:
                        report.links.append(entry["permalink"])
                except (SourceChangedError, FileNotFoundError):
                    report.pending += 1
                except OSError as error:
                    _error(report, entry, str(error))
                continue
            if entry["status"] in {"blocked", "failed"}:
                _error(report, entry, entry["error"] or "Publication is blocked")
                continue
            if entry["status"] == "publishing":
                queue.update(entry, status="uncertain", error="Previous process stopped before publication was confirmed")
            if entry["visibility"] != visibility:
                if entry["attempts"] == 0 and entry["status"] == "queued":
                    queue.update(entry, visibility=visibility)
                else:
                    _error(report, entry, "Pending publication has a different requested visibility")
                    if entry["status"] == "uncertain":
                        report.uncertain += 1
                    continue
            if entry["status"] == "queued":
                report.queued += 1
            try:
                digest = _stable_digest(source.path, settle_seconds)
                if entry["digest"] is None:
                    queue.update(entry, digest=digest)
                elif digest != entry["digest"]:
                    message = "Original changed after it was queued; publication is blocked"
                    status = "uncertain" if entry["status"] == "uncertain" else "blocked"
                    queue.update(entry, status=status, error=message)
                    _error(report, entry, message)
                    if status == "uncertain":
                        report.uncertain += 1
                    continue
            except (SourceChangedError, FileNotFoundError):
                report.pending += 1
                if entry["status"] == "uncertain":
                    report.uncertain += 1
                continue
            except OSError as error:
                _error(report, entry, str(error))
                continue
            work.append(entry)
        # Preserve visibility of old pending work whose source disappeared. A
        # missing file cannot establish a safe payload or clear uncertainty.
        if only_path is None:
            for entry in known.values():
                if entry["source_key"] in source_map or entry["status"] not in {"queued", "publishing", "uncertain"}:
                    continue
                expected_suffix = Path("userdata") / account_id / "760" / "remote" / entry["game_id"] / "screenshots" / Path(entry["path"]).name
                if not any(_path_key(root / expected_suffix) == entry["source_key"] for root in root_list):
                    continue
                message = "Queued original is unavailable; no publication was attempted"
                if entry["status"] in {"publishing", "uncertain"}:
                    queue.update(entry, status="uncertain", error=message)
                    report.uncertain += 1
                else:
                    queue.update(entry, error=message)
                    report.pending += 1
                _error(report, entry, message)
        if only_path is None and not scan_failed:
            queue.establish_baseline(scope)
        if not work:
            return report
        try:
            doctor = bridge.doctor()
            if doctor.get("ready") is not True or doctor.get("account_id") != account_id:
                raise ValueError("Steam is not ready on the pinned account; nothing was published")
            native_list = bridge.list_screenshots()
        except Exception as error:
            report.errors.append(f"Steam is unavailable: {error}")
            report.uncertain += sum(entry["status"] == "uncertain" for entry in work)
            return report
        dispatched = 0
        for entry in work:
            source = source_map[entry["source_key"]]
            was_uncertain = entry["status"] == "uncertain"
            try:
                handle = entry["native_handle"]
                if handle is None:
                    matches = []
                    for candidate in native_list:
                        try:
                            _validate_native(candidate, entry)
                            matches.append(candidate)
                        except (ValueError, OSError, AttributeError, TypeError):
                            continue
                    if len(matches) != 1:
                        raise ValueError("Expected exactly one matching native Steam screenshot")
                    handle = matches[0].handle
                    queue.update(entry, native_handle=handle)
                shot = bridge.get_screenshot(entry["game_id"], handle)
                _validate_native(shot, entry, handle=handle)
                publication, detail = _native_publication(shot, entry["visibility"])
                if publication == "unsafe":
                    queue.update(entry, status="uncertain" if was_uncertain else "blocked", error=detail)
                    _error(report, entry, detail or "Unsafe native publication metadata")
                    if was_uncertain:
                        report.uncertain += 1
                    continue
                # Native uploading reads the original, so recheck it immediately
                # before either dispatch or recording an existing publication.
                if _stable_digest(source.path, settle_seconds) != entry["digest"]:
                    raise SourceChangedError("Original changed before native publication")
                if publication == "confirmed":
                    _confirm(queue, entry, shot, detail, report)
                    continue
                if was_uncertain:
                    report.uncertain += 1
                    continue
                if entry["next_attempt"] > time.time():
                    report.pending += 1
                    continue
                if limit is not None and dispatched >= limit:
                    report.pending += 1
                    continue
                if dispatched:
                    time.sleep(1.0)
                if _stable_digest(source.path, settle_seconds) != entry["digest"]:
                    raise SourceChangedError("Original changed immediately before native publication")
                # This receipt must be durable before crossing the native call.
                queue.update(entry, status="publishing", attempts=entry["attempts"] + 1, error=None)
                dispatched += 1
                try:
                    accepted = bridge.publish(shot, visibility=entry["visibility"])
                    if type(accepted) is not bool:
                        raise ValueError("Steam returned an invalid publication result")
                except Exception as error:
                    queue.update(entry, status="uncertain", error=f"Native publication outcome is unknown: {error}")
                    report.uncertain += 1
                    _error(report, entry, entry["error"])
                    continue
                deadline = time.monotonic() + (confirm_timeout if accepted else 0)
                confirmed = False
                probes = 0
                try:
                    while True:
                        if probes and time.monotonic() >= deadline:
                            break
                        observed = bridge.get_screenshot(entry["game_id"], handle)
                        probes += 1
                        _validate_native(observed, entry, handle=handle)
                        publication, detail = _native_publication(observed, entry["visibility"])
                        if _stable_digest(source.path, settle_seconds) != entry["digest"]:
                            raise SourceChangedError("Original changed during native publication")
                        if publication == "confirmed":
                            _confirm(queue, entry, observed, detail, report)
                            confirmed = True
                            break
                        if publication == "unsafe":
                            raise ValueError(detail)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(0.5, remaining))
                except Exception as error:
                    queue.update(entry, status="uncertain", error=f"Cannot confirm native publication: {error}")
                    report.uncertain += 1
                    _error(report, entry, entry["error"])
                    continue
                if confirmed:
                    continue
                if accepted:
                    queue.update(entry, status="uncertain", error="Steam accepted the request but publication is not yet confirmed")
                    report.uncertain += 1
                else:
                    exhausted = entry["attempts"] >= MAX_ATTEMPTS
                    message = "Steam declined publication; retry limit reached" if exhausted else "Steam declined publication; retry is delayed"
                    queue.update(entry, status="failed" if exhausted else "queued", next_attempt=time.time() + 5 * 2 ** (entry["attempts"] - 1), error=message)
                    _error(report, entry, message)
                    if not exhausted:
                        report.pending += 1
            except (SourceChangedError, FileNotFoundError) as error:
                if was_uncertain:
                    report.uncertain += 1
                    queue.update(entry, status="uncertain", error=str(error))
                else:
                    queue.update(entry, status="blocked", error=str(error))
                _error(report, entry, str(error))
            except Exception as error:
                _error(report, entry, str(error))
                if was_uncertain:
                    report.uncertain += 1
        return report
