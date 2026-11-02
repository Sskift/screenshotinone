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
from .config import Config, default_config_path
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
    target = _target(config)
    stop = threading.Event()
    if watch:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        logger.info("Worker started; polling every %s seconds", config.poll_interval)
    last_summary = None
    while True:
        try:
            report = sync_once(roots, target, config.state_path,
                               settle_seconds=config.settle_seconds, dry_run=dry_run)
            summary = dataclasses.asdict(report)
            summary["dry_run"] = dry_run
            folder_url = getattr(target, "folder_url", None)
            if folder_url:
                summary["folder_url"] = folder_url
            if not watch or summary != last_summary or report.uploaded or report.errors:
                _print(summary)
                if logger:
                    logger.info("Sync: %s", json.dumps(summary, ensure_ascii=False, default=str))
            last_summary = summary
            exit_code = 1 if report.errors else 0
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            if not watch:
                raise
            logger.error("Sync pass failed: %s", exc)
            exit_code = 1
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
                    "state_path": config.state_path, "state_exists": config.state_path.exists(),
                    "log_path": config.log_path, "poll_interval": config.poll_interval})
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
