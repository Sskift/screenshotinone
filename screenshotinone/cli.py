"""Command line entry point and a quiet, polling background worker."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from . import __version__
from .config import Config, SteamConfig, default_config_path
from .engine import sync_once
from .source import discover_steam_roots, scan_screenshots
from .targets import FolderTarget, NetclipTarget


def _print(value: object) -> None:
    if sys.stdout is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automatically sync Steam screenshots without desktop interaction.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=default_config_path(), help="JSON configuration path")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create configuration; never overwrite an existing file")
    init.add_argument("--target", choices=("netclip", "folder"), default="netclip")
    init.add_argument("--url", help="Netclip server origin")
    init.add_argument("--folder", default="Steam Screenshots", help="Netclip root folder name")
    init.add_argument("--destination", type=Path, help="Existing cloud client's local sync folder")
    init.add_argument("--steam-root", type=Path, action="append", default=[], help="Steam installation path; repeatable")
    commands.add_parser("scan", help="List discovered Steam screenshots (no configuration required)")
    sync = commands.add_parser("sync", help="Run one synchronization pass")
    sync.add_argument("--dry-run", action="store_true", help="Plan without uploading or changing state")
    commands.add_parser("watch", help="Poll for new screenshots; retry failed files on later passes")
    commands.add_parser("status", help="Show source counts and local worker log location")
    steam = commands.add_parser("steam", help="Experimental Steam Community publication through the local Steam client")
    steam_commands = steam.add_subparsers(dest="steam_command", required=True)
    doctor = steam_commands.add_parser("doctor", help="Read-only check of Steam's local debugging interface and account")
    doctor.add_argument("--endpoint", help="Local Steam CDP HTTP origin (numeric loopback only)")
    steam_commands.add_parser("status", help="Read the durable publication queue without connecting to Steam")
    steam_sync = steam_commands.add_parser("sync", help="Run one Steam-only publication pass")
    steam_sync.add_argument("--dry-run", action="store_true")
    steam_sync.add_argument("--include-existing", action="store_true", help="Explicitly include historical screenshots")
    steam_sync.add_argument("--limit", type=int, help="Maximum number of upload calls this pass")
    publish = steam_commands.add_parser("publish", help="Publish one explicitly selected Steam screenshot")
    publish.add_argument("--file", type=Path, required=True)
    publish.add_argument("--visibility", choices=("private", "friends", "public", "unlisted"))
    publish.add_argument("--dry-run", action="store_true")
    return parser


def _init(args: argparse.Namespace) -> int:
    if args.target == "netclip":
        if not args.url:
            raise ValueError("init --target netclip requires --url")
        target = {"type": "netclip", "url": args.url, "folder": args.folder}
    else:
        if not args.destination:
            raise ValueError("init --target folder requires --destination")
        target = {"type": "folder", "path": str(args.destination.expanduser().resolve())}
    data = {"steam_roots": [str(root.expanduser().resolve()) for root in args.steam_root],
            "target": target, "poll_interval": 15, "settle_seconds": 3}
    path = args.config.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents accidentally replacing a working destination.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        Config.load(path)
    except (ValueError, OSError):
        path.unlink()
        raise
    _print({"config": path, "next": "Run scan, then sync --dry-run, then sync or watch."})
    return 0


def _roots(config: Config | None) -> list[Path]:
    roots = list(config.steam_roots) if config and config.steam_roots else discover_steam_roots()
    if not roots:
        raise ValueError("Steam was not found. Set steam_roots to your Steam installation directory.")
    for root in roots:
        if not (root / "userdata").is_dir():
            raise ValueError(f"Steam userdata directory not found: {root / 'userdata'}")
    return roots


def _target(config: Config):
    return NetclipTarget(**config.target_options) if config.target_type == "netclip" else FolderTarget(**config.target_options)


def _steam_pass(config: Config, *, dry_run: bool = False, include_existing: bool = False,
                only_path: Path | None = None, visibility: str | None = None, limit: int | None = None):
    from .cdp import SteamCDP
    from .steam_bridge import SteamBridge
    from .steam_sync import steam_sync_once

    settings = config.steam
    if settings is None or settings.account_id is None:
        raise ValueError("Configure steam.account_id before publishing; run steam doctor to identify the logged-in account.")
    if limit is not None and limit <= 0:
        raise ValueError("--limit must be a positive integer")
    bridge = SteamBridge(SteamCDP(settings.endpoint, timeout=settings.timeout), timeout=settings.timeout)
    try:
        return steam_sync_once(
            _roots(config), bridge, settings.state_path,
            account_id=settings.account_id, visibility=visibility or settings.visibility,
            include_existing=include_existing or settings.include_existing, dry_run=dry_run,
            limit=limit, only_path=only_path,
            settle_seconds=config.settle_seconds, confirm_timeout=settings.confirm_timeout,
        )
    finally:
        bridge.close()


def _steam_command(args: argparse.Namespace, config: Config | None) -> int:
    settings = config.steam if config and config.steam else SteamConfig()
    if args.steam_command == "doctor":
        from .cdp import SteamCDP
        from .steam_bridge import SteamBridge

        bridge = SteamBridge(SteamCDP(args.endpoint or settings.endpoint, timeout=settings.timeout), timeout=settings.timeout)
        try:
            details = bridge.doctor()
            account = details.get("account_id")
            matches = settings.account_id is None or settings.account_id == account
            _print({**details, "read_only": True, "configured_account_id": settings.account_id,
                    "account_matches": matches, "automatic_enabled": settings.enabled})
            return 0 if details.get("ready") is True and matches else 1
        except (OSError, RuntimeError, ValueError) as exc:
            _print({"available": False, "read_only": True, "error": str(exc),
                    "next": "Start Steam with -cef-enable-debugging, then retry. No Steam restart or publication was performed."})
            return 1
        finally:
            bridge.close()
    if args.steam_command == "status":
        from .steam_sync import steam_queue_status

        state_path = settings.state_path or args.config.parent / "steam-state.sqlite3"
        _print({"automatic_enabled": settings.enabled, "visibility": settings.visibility,
                "account_id": settings.account_id, "state_path": state_path,
                "queue": steam_queue_status(state_path, account_id=settings.account_id)})
        return 0
    if config is None:
        raise ValueError("Configuration not found. Configure the Steam account and destination first.")
    manual = args.steam_command == "publish"
    report = _steam_pass(config, dry_run=args.dry_run,
                         include_existing=manual or getattr(args, "include_existing", False),
                         only_path=args.file.expanduser().resolve() if manual else None,
                         visibility=getattr(args, "visibility", None),
                         limit=1 if manual else args.limit)
    summary = {**dataclasses.asdict(report), "dry_run": args.dry_run}
    _print(summary)
    if not args.dry_run:
        _logger(config).info("Steam sync: %s", json.dumps(summary, ensure_ascii=False, default=str))
    return 1 if report.errors or report.uncertain else 0


def _logger(config: Config) -> logging.Logger:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("screenshotinone")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(config.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _run(config: Config, *, watch: bool, dry_run: bool = False) -> int:
    logger = _logger(config) if not dry_run else None
    roots = _roots(config)
    target = _target(config) if config.target_enabled else None
    stop = threading.Event()
    if watch:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        logger.info("Worker started; polling every %s seconds", config.poll_interval)
    last_summary = None
    while True:
        summary = {"dry_run": dry_run, "target_enabled": config.target_enabled}
        exit_code = 0
        changed = False
        if target is not None:
            try:
                report = sync_once(roots, target, config.state_path,
                                   settle_seconds=config.settle_seconds, dry_run=dry_run)
                summary.update(dataclasses.asdict(report))
                folder_url = getattr(target, "folder_url", None)
                if folder_url:
                    summary["folder_url"] = folder_url
                exit_code = 1 if report.errors else 0
                changed = bool(report.uploaded)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                summary["errors"] = [str(exc)]
                exit_code = 1
        if config.steam and config.steam.enabled:
            try:
                steam_report = _steam_pass(config, dry_run=dry_run)
                summary["steam"] = dataclasses.asdict(steam_report)
                changed = changed or bool(steam_report.uploaded)
                if steam_report.errors or steam_report.uncertain:
                    exit_code = 1
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                summary["steam"] = {"errors": [str(exc)]}
                exit_code = 1
        if not watch or summary != last_summary or changed:
            _print(summary)
            if logger:
                logger.info("Sync: %s", json.dumps(summary, ensure_ascii=False, default=str))
        last_summary = summary
        if not watch:
            return exit_code
        if stop.wait(config.poll_interval):
            logger.info("Worker stopped")
            return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.config = args.config.expanduser().resolve()
    config = None
    try:
        if args.command == "init":
            return _init(args)
        config = Config.load(args.config) if args.config.exists() else None
        if args.command == "steam":
            return _steam_command(args, config)
        if args.command == "scan":
            roots = _roots(config)
            screenshots = scan_screenshots(roots)
            _print({"steam_roots": roots, "count": len(screenshots), "screenshots": [
                {"path": shot.path, "account_id": shot.account_id, "app_id": shot.app_id}
                for shot in screenshots]})
            return 0
        if config is None:
            raise ValueError(f"Configuration not found: {args.config}. Run init first.")
        if args.command == "status":
            roots = _roots(config)
            _print({"version": __version__, "config": config.path, "steam_roots": roots,
                    "screenshots": len(scan_screenshots(roots)), "target": config.target_type,
                    "target_enabled": config.target_enabled,
                    "state_path": config.state_path, "state_exists": config.state_path.exists(),
                    "log_path": config.log_path, "poll_interval": config.poll_interval,
                    "steam_enabled": bool(config.steam and config.steam.enabled)})
            return 0
        return _run(config, watch=args.command == "watch", dry_run=getattr(args, "dry_run", False))
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if sys.stderr is not None:
            print(f"screenshotinone: {exc}", file=sys.stderr)
        if args.command == "watch":
            # pythonw has no stderr: persist bootstrap errors as well as pass errors.
            try:
                if config is not None:
                    _logger(config).error("Worker could not start: %s", exc)
                else:
                    args.config.parent.mkdir(parents=True, exist_ok=True)
                    with (args.config.parent / "sync.log").open("a", encoding="utf-8") as handle:
                        handle.write(f"Worker could not load configuration: {exc}\n")
            except OSError:
                pass
        return 1
    except KeyboardInterrupt:
        return 130
