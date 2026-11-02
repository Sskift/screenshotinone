"""Find Steam screenshot originals without changing Steam's files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import stat
import sys
from typing import Callable, Iterable


IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
ScanErrorHandler = Callable[[Path, OSError], None]


@dataclass(frozen=True)
class Screenshot:
    path: Path
    account_id: str
    app_id: str

    @property
    def relative_path(self) -> str:
        """An unambiguous path across Steam accounts and games."""
        return f"{self.account_id}/{self.app_id}/{self.path.name}"


def _is_link(path: Path) -> bool:
    """Include Windows directory junctions, which are not is_symlink()."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _plain_directory(path: Path) -> bool:
    return not _is_link(path) and path.is_dir()


def _report_scan_error(path: Path, error: OSError, on_error: ScanErrorHandler | None) -> None:
    if on_error is None:
        raise error
    on_error(path, error)


def _directory_exists(path: Path, on_error: ScanErrorHandler | None) -> bool:
    try:
        return _plain_directory(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        _report_scan_error(path, error, on_error)
        return False


def _children(parent: Path, on_error: ScanErrorHandler | None) -> list[Path]:
    try:
        return sorted(parent.iterdir())
    except FileNotFoundError:
        return []
    except OSError as error:
        _report_scan_error(parent, error, on_error)
        return []


def _numeric_directories(parent: Path, on_error: ScanErrorHandler | None) -> list[Path]:
    return [
        path
        for path in _children(parent, on_error)
        if path.name.isascii()
        and path.name.isdecimal()
        and _directory_exists(path, on_error)
    ]


def discover_steam_roots() -> list[Path]:
    """Return existing Steam installation roots, with duplicates removed.

    Screenshots belong to the main client installation's userdata directory,
    rather than each game's separate Steam library.
    """
    candidates: list[Path] = []
    if os.environ.get("STEAM_PATH"):
        candidates.append(Path(os.environ["STEAM_PATH"]))
    if sys.platform == "win32":
        import winreg

        registry_keys = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        )
        for hive, key_path, value_name in registry_keys:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
                if isinstance(value, str) and value:
                    candidates.append(Path(value))
            except OSError:
                pass
        for variable, fallback in (
            ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            ("ProgramFiles", r"C:\Program Files"),
        ):
            candidates.append(Path(os.environ.get(variable, fallback)) / "Steam")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library/Application Support/Steam")
    else:
        candidates.extend(
            Path.home() / relative
            for relative in (
                ".local/share/Steam",
                ".steam/steam",
                ".steam/root",
                ".var/app/com.valvesoftware.Steam/.local/share/Steam",
            )
        )
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            key = os.path.normcase(str(resolved))
            if key not in seen and (resolved / "userdata").is_dir():
                seen.add(key)
                roots.append(resolved)
        except OSError:
            continue
    return roots


def scan_screenshots(
    roots: Iterable[Path], *, on_error: ScanErrorHandler | None = None
) -> list[Screenshot]:
    """List image originals directly inside Steam screenshot directories.

    Thumbnails, unrelated images, symbolic links, and directory junctions are
    deliberately excluded. By default unreadable paths raise OSError. With an
    on_error callback, each failure is reported and healthy folders continue.
    """
    screenshots: list[Screenshot] = []
    seen: set[str] = set()
    for configured_root in roots:
        try:
            root = Path(configured_root).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise NotADirectoryError(str(root))
        except OSError as error:
            _report_scan_error(Path(configured_root), error, on_error)
            continue
        userdata = root / "userdata"
        if not _directory_exists(userdata, on_error):
            continue
        for account in _numeric_directories(userdata, on_error):
            service = account / "760"
            remote = service / "remote"
            if not _directory_exists(service, on_error):
                continue
            if not _directory_exists(remote, on_error):
                continue
            for game in _numeric_directories(remote, on_error):
                originals = game / "screenshots"
                if not _directory_exists(originals, on_error):
                    continue
                for image in _children(originals, on_error):
                    if image.suffix.lower() not in IMAGE_SUFFIXES:
                        continue
                    try:
                        if _is_link(image) or not image.is_file():
                            continue
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        _report_scan_error(image, error, on_error)
                        continue
                    key = os.path.normcase(str(image))
                    if key not in seen:
                        seen.add(key)
                        screenshots.append(Screenshot(image, account.name, game.name))
    return screenshots
