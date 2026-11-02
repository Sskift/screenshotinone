"""Small, explicit JSON configuration; no service addresses baked into the app."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "screenshotinone" / "config.json"


def _path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _number(data: dict, name: str, default: float, minimum: float) -> float:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return float(value)


@dataclass(frozen=True)
class SteamConfig:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:8080"
    account_id: str | None = None
    visibility: str = "private"
    include_existing: bool = False
    timeout: float = 10
    confirm_timeout: float = 10
    state_path: Path | None = None

    @classmethod
    def parse(cls, value: object, base: Path) -> SteamConfig:
        if not isinstance(value, dict):
            raise ValueError("steam must be an object")
        allowed = {"enabled", "endpoint", "account_id", "visibility", "include_existing",
                   "timeout", "confirm_timeout", "state_path"}
        if set(value) - allowed:
            raise ValueError("Unknown Steam options: " + ", ".join(sorted(set(value) - allowed)))
        for name in ("enabled", "include_existing"):
            if name in value and not isinstance(value[name], bool):
                raise ValueError(f"steam.{name} must be a boolean")
        enabled = value.get("enabled", False)
        account_id = value.get("account_id")
        if account_id is not None and (not isinstance(account_id, str) or not account_id.isascii()
                                       or not account_id.isdecimal() or not 0 < int(account_id) < 2**32):
            raise ValueError("steam.account_id must be the numeric Steam userdata account folder, as a string")
        if enabled and account_id is None:
            raise ValueError("steam.account_id is required before enabling automatic publication")
        visibility = value.get("visibility", "private")
        if not isinstance(visibility, str) or visibility not in {"private", "friends", "public", "unlisted"}:
            raise ValueError("steam.visibility must be private, friends, public, or unlisted")
        endpoint = value.get("endpoint", "http://127.0.0.1:8080")
        if not isinstance(endpoint, str):
            raise ValueError("steam.endpoint must be a loopback HTTP URL")
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("steam.endpoint must use http://127.0.0.1 or http://[::1] with no credentials or path")
        try:
            port = parsed.port
            if port is not None and port == 0:
                raise ValueError()
        except ValueError as exc:
            raise ValueError("steam.endpoint has an invalid port") from exc
        state_path = value.get("state_path", "steam-state.sqlite3")
        if not isinstance(state_path, str) or not state_path.strip():
            raise ValueError("steam.state_path must be a nonempty path")
        return cls(enabled=enabled, endpoint=endpoint.rstrip("/"), account_id=account_id,
                   visibility=visibility, include_existing=value.get("include_existing", False),
                   timeout=_number(value, "timeout", 10, 1),
                   confirm_timeout=_number(value, "confirm_timeout", 10, 1),
                   state_path=_path(state_path, base))


@dataclass(frozen=True)
class Config:
    path: Path
    steam_roots: tuple[Path, ...]
    target_type: str
    target_options: dict
    state_path: Path
    log_path: Path
    poll_interval: float
    settle_seconds: float
    steam: SteamConfig | None = None
    target_enabled: bool = True

    @classmethod
    def load(cls, path: Path) -> Config:
        path = path.expanduser().resolve()
        with path.open(encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("Configuration must be a JSON object")
        allowed = {"steam_roots", "target", "state_path", "log_path", "poll_interval", "settle_seconds", "steam"}
        if set(data) - allowed:
            raise ValueError("Unknown config keys: " + ", ".join(sorted(set(data) - allowed)))
        roots = data.get("steam_roots", [])
        if not isinstance(roots, list) or any(not isinstance(item, str) or not item for item in roots):
            raise ValueError("steam_roots must be a list of Steam installation paths")
        target = data.get("target")
        if not isinstance(target, dict):
            raise ValueError("target must be an object")
        target_enabled = target.get("enabled", True)
        if not isinstance(target_enabled, bool):
            raise ValueError("target.enabled must be a boolean")
        kind = target.get("type")
        if kind == "netclip":
            if set(target) - {"type", "url", "folder", "timeout", "enabled"}:
                raise ValueError("Unknown Netclip target option")
            url = target.get("url", "")
            if not isinstance(url, str):
                raise ValueError("Netclip url must be a string")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Netclip url must be an http:// or https:// server address")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Netclip url must not contain credentials, a query, or a fragment")
            if parsed.path not in {"", "/"}:
                raise ValueError("Netclip url must be the server origin, without a path")
            try:
                parsed.port
            except ValueError as exc:
                raise ValueError("Invalid Netclip server port") from exc
            folder = target.get("folder", "Steam Screenshots")
            if not isinstance(folder, str) or not folder.strip() or folder.strip() in {".", ".."}:
                raise ValueError("Netclip folder must be a nonempty folder name")
            if any(ord(c) < 32 or c in "/\\" for c in folder) or len(folder.encode("utf-8")) > 240:
                raise ValueError("Netclip folder contains invalid characters or exceeds 240 UTF-8 bytes")
            options = {"base_url": url.rstrip("/"), "folder_name": folder.strip(),
                       "timeout": _number(target, "timeout", 30, 1)}
        elif kind == "folder":
            if set(target) - {"type", "path", "enabled"}:
                raise ValueError("Unknown folder target option")
            dest = target.get("path")
            if not isinstance(dest, str) or not dest.strip():
                raise ValueError("Folder target requires a path")
            options = {"destination": _path(dest, path.parent)}
        else:
            raise ValueError("target.type must be 'netclip' or 'folder'")
        for name in ("state_path", "log_path"):
            if name in data and (not isinstance(data[name], str) or not data[name].strip()):
                raise ValueError(f"{name} must be a nonempty path")
        main_state_path = _path(data.get("state_path", "state.sqlite3"), path.parent)
        steam = SteamConfig.parse(data["steam"], path.parent) if "steam" in data else None
        if steam and steam.state_path == main_state_path:
            raise ValueError("steam.state_path must be separate from state_path")
        return cls(
            path=path,
            steam_roots=tuple(_path(item, path.parent) for item in roots),
            target_type=kind,
            target_options=options,
            state_path=main_state_path,
            log_path=_path(data.get("log_path", "sync.log"), path.parent),
            poll_interval=_number(data, "poll_interval", 15, 1),
            settle_seconds=_number(data, "settle_seconds", 3, 0),
            steam=steam,
            target_enabled=target_enabled,
        )
