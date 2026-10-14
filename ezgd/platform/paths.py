"""Per-user application directories (XDG on Linux, %APPDATA% on Windows)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ezgd import APP_NAME
from ezgd.platform import IS_WINDOWS


@dataclass(frozen=True)
class AppPaths:
    data: Path
    config: Path
    cache: Path
    logs: Path

    @property
    def database(self) -> Path:
        return self.data / "ezgd.sqlite3"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def covers(self) -> Path:
        return self.cache / "covers"

    @property
    def torrents(self) -> Path:
        """Copies of .torrent files for queued/active torrents (needed for recovery)."""
        return self.data / "torrents"

    @property
    def prefixes(self) -> Path:
        """Per-game Proton/Wine prefixes on Linux."""
        return self.data / "prefixes"

    @property
    def reports(self) -> Path:
        return self.data / "reports"

    @property
    def save_backups(self) -> Path:
        """Zipped save games, one folder per game title (see ezgd.library.saves)."""
        return self.data / "save-backups"

    @property
    def updates(self) -> Path:
        """Downloaded app updates (removed once installed)."""
        return self.data / "updates"

    @property
    def config_file(self) -> Path:
        return self.config / "config.toml"

    def ensure(self) -> None:
        for p in (self.data, self.config, self.cache, self.logs, self.backups,
                  self.covers, self.torrents, self.prefixes, self.reports, self.save_backups):
            p.mkdir(parents=True, exist_ok=True)


def resolve_paths(home: str | os.PathLike | None = None) -> AppPaths:
    """Return the app directories.

    ``home`` (or the ``EZGD_HOME`` environment variable) switches to a portable
    layout where everything lives under one directory; tests use this too.
    """
    home = home or os.environ.get("EZGD_HOME")
    if home:
        base = Path(home).expanduser().resolve()
        return AppPaths(base / "data", base / "config", base / "cache", base / "logs")
    if IS_WINDOWS:
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return AppPaths(roaming / APP_NAME, roaming / APP_NAME,
                        local / APP_NAME / "cache", local / APP_NAME / "logs")
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return AppPaths(xdg_data / APP_NAME, xdg_config / APP_NAME,
                    xdg_cache / APP_NAME, xdg_state / APP_NAME / "logs")


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "EZ Games"


def default_install_dir() -> Path:
    return Path.home() / "Games"
