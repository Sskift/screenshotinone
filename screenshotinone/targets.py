"""Append-only destinations for stable Steam screenshot snapshots."""

from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata
from urllib import error, parse, request


class TargetError(RuntimeError):
    """A destination could not safely accept a screenshot."""


def _digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _filename(screenshot, digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise TargetError("Expected a lowercase SHA-256 content digest")
    source = Path(screenshot.path)
    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    stem = unicodedata.normalize("NFC", source.stem)
    stem = re.sub(r'[\\/\x00-\x1f\x7f<>:"|?*]', "_", stem).strip(" .") or "screenshot"
    # Netclip permits 240 UTF-8 bytes. Leave room for the digest and extension.
    budget = 240 - len(("-" + digest + suffix).encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return f"{stem}-{digest}{suffix}"


def _components(screenshot) -> tuple[str, str]:
    values = str(screenshot.account_id), str(screenshot.app_id)
    if any(not re.fullmatch(r"[0-9]{1,20}", part) for part in values):
        raise TargetError("Steam account and app IDs must be numeric path components")
    return values


def _snapshot(path: Path, digest: str) -> int:
    try:
        size = path.stat().st_size
        if _digest_file(path) != digest:
            raise TargetError("Screenshot snapshot does not match its content digest")
        return size
    except OSError as exc:
        raise TargetError("Cannot read the screenshot snapshot") from exc


class FolderTarget:
    """Copy originals to a folder, suitable for an existing cloud sync client."""

    def __init__(self, destination: Path):
        self.destination = Path(destination).expanduser().resolve()
        self.identity = f"folder:{self.destination.as_posix()}"

    def upload(self, screenshot, snapshot_path: Path, digest: str) -> str:
        name = _filename(screenshot, digest)
        components = _components(screenshot)
        snapshot_path = Path(snapshot_path)
        _snapshot(snapshot_path, digest)
        parent = self.destination.joinpath(*components)
        # Refuse symlink/junction escapes from a user-supplied destination.
        if not parent.resolve().is_relative_to(self.destination):
            raise TargetError("Destination subfolder resolves outside the sync folder")
        target = parent / name
        temporary: Path | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file() or _digest_file(target) != digest:
                    raise TargetError("A different file already occupies the screenshot destination")
                return str(target)
            with tempfile.NamedTemporaryFile(prefix=".screenshotinone-", suffix=".tmp", dir=parent, delete=False) as stream:
                temporary = Path(stream.name)
                with snapshot_path.open("rb") as source:
                    shutil.copyfileobj(source, stream, length=1024 * 1024)
                stream.flush()
                os.fsync(stream.fileno())
            if _digest_file(temporary) != digest:
                raise TargetError("Screenshot snapshot changed during copying")
            # A hard link atomically publishes complete bytes without overwriting.
            # Local NTFS and normal cloud-sync folders support this operation.
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or _digest_file(target) != digest:
                    raise TargetError("A different file appeared at the screenshot destination")
            return str(target)
        except OSError as exc:
            raise TargetError("Cannot copy screenshot to destination (requires a writable folder with hard-link support)") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fold_name(value: str) -> str:
    # Match Netclip's SQLite NOCASE comparison, which folds ASCII only.
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


class NetclipTarget:
    """Upload to Netclip Drive, reconciling completed uploads before retrying."""

    def __init__(self, base_url: str, folder_name: str = "Steam Screenshots", timeout: float = 30):
        parts = parse.urlsplit(base_url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Netclip URL must be an HTTP(S) base URL without credentials, query, or fragment")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError("Netclip URL has an invalid port") from exc
        folder_name = unicodedata.normalize("NFC", folder_name.strip())
        if (not folder_name or folder_name in {".", ".."} or re.search(r"[/\\\x00-\x1f\x7f]", folder_name)
                or len(folder_name.encode("utf-8")) > 240):
            raise ValueError("Netclip folder name must be 1–240 UTF-8 bytes without slashes or control characters")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Netclip timeout must be a positive finite number")
        self.base_url = parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        self.folder_name = folder_name
        self.timeout = timeout
        self.identity = f"netclip:{self.base_url}:{folder_name}"
        self.folder_url: str | None = None
        self._opener = request.build_opener(_NoRedirect())

    def _open(self, method: str, path: str, *, data=None, headers=None):
        req = request.Request(self.base_url + path, data=data, headers=headers or {}, method=method)
        try:
            return self._opener.open(req, timeout=self.timeout)
        except error.HTTPError:
            raise
        except (OSError, error.URLError, HTTPException) as exc:
            raise TargetError("Cannot reach Netclip; check the server and network connection") from exc

    def _json(self, method: str, path: str, *, body=None) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with self._open(method, path, data=data, headers=headers) as response:
            try:
                value = json.load(response)
            except (ValueError, OSError) as exc:
                raise TargetError("Netclip returned an invalid JSON response") from exc
        if not isinstance(value, dict):
            raise TargetError("Netclip returned an unexpected response")
        return value

    def _entries(self, parent: int | None) -> dict:
        suffix = "" if parent is None else f"?parent={parent}"
        result = self._json("GET", "/api/drive/entries" + suffix)
        if not isinstance(result.get("entries"), list):
            raise TargetError("Netclip Drive did not return a folder listing")
        return result

    @staticmethod
    def _id(entry: dict) -> int:
        identifier = entry.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise TargetError("Netclip returned an invalid entry ID")
        return identifier

    def _folder(self, name: str, parent: int | None, listing: dict | None = None) -> int:
        listing = listing if listing is not None else self._entries(parent)
        matches = [entry for entry in listing["entries"] if _fold_name(str(entry.get("name", ""))) == _fold_name(name)]
        if matches:
            if len(matches) != 1 or matches[0].get("kind") != "folder":
                raise TargetError("A file already occupies a required Netclip folder name")
            return self._id(matches[0])
        try:
            result = self._json("POST", "/api/drive/folders", body={"name": name, "parentId": parent})
        except error.HTTPError as exc:
            if exc.code != 409:
                raise
            exc.close()
            # A different client may have created the folder after our listing.
            refreshed = self._entries(parent)
            for entry in refreshed["entries"]:
                if _fold_name(str(entry.get("name", ""))) == _fold_name(name) and entry.get("kind") == "folder":
                    return self._id(entry)
            raise TargetError("Netclip folder name conflicts with an existing entry") from None
        entry = result.get("entry")
        if not isinstance(entry, dict) or entry.get("kind") != "folder":
            raise TargetError("Netclip did not confirm folder creation")
        return self._id(entry)

    def _verify_existing(self, entry: dict, size: int, digest: str) -> str:
        identifier = self._id(entry)
        if entry.get("kind") != "file" or entry.get("bytes") != size:
            raise TargetError("Netclip already contains different content with this screenshot's name")
        with self._open("GET", f"/api/drive/entries/{identifier}/download") as response:
            checksum = hashlib.sha256()
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > size:
                    raise TargetError("Netclip screenshot content did not match its expected size")
                checksum.update(chunk)
        if count != size or checksum.hexdigest() != digest:
            raise TargetError("Netclip screenshot content did not match its expected SHA-256")
        return f"{self.base_url}/api/drive/entries/{identifier}/download"

    def upload(self, screenshot, snapshot_path: Path, digest: str) -> str:
        name = _filename(screenshot, digest)
        account, app = _components(screenshot)
        snapshot_path = Path(snapshot_path)
        size = _snapshot(snapshot_path, digest)
        try:
            root = self._entries(None)
            limit = root.get("maxUploadBytes")
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise TargetError("Netclip did not report a valid Drive upload limit")
            if size > limit:
                raise TargetError(f"Screenshot exceeds Netclip Drive upload limit ({limit} bytes)")
            destination = self._folder(self.folder_name, None, root)
            self.folder_url = f"{self.base_url}/?view=drive&folder={destination}"
            parent = self._folder(app, self._folder(account, destination))
            for entry in self._entries(parent)["entries"]:
                if _fold_name(str(entry.get("name", ""))) == _fold_name(name):
                    return self._verify_existing(entry, size, digest)
            headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size),
                       "X-Filename": parse.quote(name, safe=""), "Accept": "application/json"}
            with snapshot_path.open("rb") as stream:
                with self._open("POST", f"/api/drive/upload?parent={parent}", data=stream, headers=headers) as response:
                    try:
                        result = json.load(response)
                    except (ValueError, OSError) as exc:
                        raise TargetError("Netclip upload confirmation was unreadable; the next sync will reconcile it") from exc
            entry = result.get("entry") if isinstance(result, dict) else None
            if not isinstance(entry, dict) or entry.get("kind") != "file" or entry.get("bytes") != size or entry.get("name") != name:
                raise TargetError("Netclip upload confirmation did not match; check for concurrent uploaders")
            return f"{self.base_url}/api/drive/entries/{self._id(entry)}/download"
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise TargetError(f"Netclip request failed (HTTP {status})") from None
        except (OSError, error.URLError, HTTPException) as exc:
            raise TargetError("Netclip transfer was interrupted; the next sync will reconcile completed uploads") from exc
