"""Typed application settings persisted in the ``settings`` table."""

from __future__ import annotations

import json
import re
import logging
import os
import threading
import time
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from ezgd.platform.paths import default_download_dir, default_install_dir

log = logging.getLogger(__name__)

ACCENTS = {
    "violet": "#8b5cf6",
    "cyan": "#22d3ee",
    "emerald": "#10b981",
    "crimson": "#f43f5e",
    "amber": "#f59e0b",
}


@dataclass
class Settings:
    # paths
    download_dir: str = field(default_factory=lambda: str(default_download_dir()))
    install_dir: str = field(default_factory=lambda: str(default_install_dir()))
    # one folder: a game's download goes into its own folder in install_dir, where it is also extracted — with
    # "delete the archive" on, only the game is left (download_dir is then not used)
    downloads_in_game_folder: bool = True
    # queue / http
    max_concurrent_downloads: int = 3
    http_speed_limit_kbps: int = 0              # 0 = unlimited (shared by all HTTP transfers)
    download_hours: bool = False                 # only download between download_from and download_to
    download_from: str = "01:00"
    download_to: str = "08:00"
    play_speed_limit_kbps: int = 0               # while a game runs, downloads go at most this fast (0 = as usual)
    http_max_retries: int = 5
    http_timeout_seconds: int = 30
    # torrent
    torrent_listen_port: int = 6881              # 0 = random port
    torrent_max_connections: int = 200
    torrent_download_limit_kbps: int = 0
    torrent_upload_limit_kbps: int = 0
    torrent_enable_dht: bool = True
    torrent_enable_lsd: bool = True
    torrent_enable_upnp: bool = True
    torrent_seed_after_complete: bool = False
    torrent_stall_minutes: int = 10
    # post-processing
    auto_extract: bool = True
    delete_archive_after_extract: bool = False
    extract_into_install_dir: bool = True        # False → extract next to the download
    unrar_tool: str = ""                         # optional explicit path to unrar/7z
    # sources
    sources_enabled: dict = field(default_factory=lambda: {"onlinefix": True, "steamrip": True,
                                                             "crackedgames": True, "fitgirl": True,
                                                             "ankergames": True})
    hide_withdrawn_games: bool = True            # hide OnlineFix games whose guide was closed
    hide_adult_games: bool = True                # hide games a source marks 18+ (cracked-games.org)
    browser_adblock: bool = True                 # in-app browser: block ad/tracker domains and pop-unders
    browser_adblock_auto_update: bool = True     # keep the ad blocklist current (checked once a day)
    protondb_fixes: bool = True                  # debugging assistant: use ProtonDB players' fixes (~70 MB/month)
    cache_ttl_minutes: int = 60
    cover_cache_mb: int = 200
    # library / launch
    confirm_before_launch: bool = True
    launch_through_steam: bool = True            # start games through Steam shortcuts (full Steam overlay)
    steam_overlay: bool = True                   # load Steam's overlay into games (friends list only)
    proton_version: str = ""                     # "" = newest detected
    steam_root: str = ""                         # "" = auto-detect
    check_game_updates: bool = True              # re-check installed games' pages for newer versions (every 6 h)
    auto_update_games: bool = False              # download and install new versions by themselves
    auto_backup_saves: bool = True               # back up saves after playing and before updates/uninstalls
    save_sync_dir: str = ""                      # save backups go here too (a Syncthing / Nextcloud / Drive folder)
    # app updates (GitHub releases)
    update_repo: str = ""                        # "owner/name" with the app's releases; empty = the official one
    check_app_updates: bool = True               # look for a new version of the app once a day
    start_with_session: bool = False             # start (in the tray) when the computer starts / you sign in
    # ui
    theme_accent: str = "violet"
    theme_contrast: str = "dark"                 # dark | midnight
    card_size: str = "comfortable"               # compact | comfortable | large
    reduce_motion: bool = False
    # couch mode display (when the automatic full screen does not fit a setup)
    couch_screen: str = ""                       # "" = the screen the app is on; else a screen's name ("DP-1")
    couch_window: str = "fullscreen"             # fullscreen | borderless | custom
    couch_resolution: str = ""                   # custom size in screen pixels, "2560x1440"
    couch_ui_scale: int = 100                    # interface size in percent (100 = fitted to the screen)
    couch_sounds: bool = True                    # couch mode's quiet sounds (selector moves, presses, browser clicks)
    couch_sounds_volume: int = 15                # … how loud, in percent
    couch_intro_volume: int = 17                 # the intro video's sound, in percent
    couch_steady_frames: bool = True             # one steady frame rate (no flicker on VRR screens)
    mangohud_live: bool = True                   # MangoHud loaded (hidden) in every game: on/off live from the menu
    gpu_acceleration: bool = False               # couch mode drawn by the GPU; GPU help for the in-app browser
    couch_gpu: bool = True                       # (no longer used: part of gpu_acceleration since 0.5.3)
    couch_controller_only: bool = True           # couch Store: only games with (full or partial) controller support
    couch_library_sort: str = "title"            # couch Library order: title | recent | added | playtime
    couch_intro: bool = True                     # the 2-second intro video when couch mode opens (any button skips)
    couch_intro_sound: bool = True               # … with its sound
    notifications_enabled: bool = True
    # diagnostics
    log_level: str = "INFO"

    _LIMITS = {
        "max_concurrent_downloads": (1, 16),
        "couch_sounds_volume": (0, 100),
        "couch_intro_volume": (0, 100),
        "http_speed_limit_kbps": (0, 10_000_000),
        "play_speed_limit_kbps": (0, 10_000_000),
        "http_max_retries": (0, 20),
        "http_timeout_seconds": (5, 300),
        "torrent_listen_port": (0, 65535),
        "torrent_max_connections": (10, 5000),
        "torrent_download_limit_kbps": (0, 10_000_000),
        "torrent_upload_limit_kbps": (0, 10_000_000),
        "torrent_stall_minutes": (1, 240),
        "cache_ttl_minutes": (1, 7 * 24 * 60),
        "cover_cache_mb": (10, 10_000),
        "couch_ui_scale": (50, 250),
    }
    _CHOICES = {
        "theme_accent": tuple(ACCENTS),
        "theme_contrast": ("dark", "midnight"),
        "card_size": ("compact", "comfortable", "large"),
        "log_level": ("DEBUG", "INFO", "WARNING", "ERROR"),
        "couch_window": ("fullscreen", "borderless", "custom"),
        "couch_library_sort": ("title", "recent", "added", "playtime"),
    }

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def coerce(cls, key: str, value: Any) -> Any:
        """Validate and convert a value for ``key``; raises ValueError/KeyError."""
        f = {f.name: f for f in fields(cls)}[key]
        default = getattr(cls(), key)
        if isinstance(default, bool):
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
            lo, hi = cls._LIMITS.get(key, (None, None))
            if lo is not None:
                value = max(lo, min(hi, value))
        elif isinstance(default, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a table/dict")
            value = {str(k): bool(v) for k, v in value.items()}
        else:
            value = str(value)
            if key.endswith("_dir") or key in ("steam_root", "unrar_tool"):
                value = os.path.expanduser(value) if value else value
            if key in ("download_from", "download_to"):
                m = re.fullmatch(r"(\d{1,2})[:.h](\d{2})", value.strip())
                if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                    raise ValueError(f"{key} must be a time like 01:30")
                value = f"{int(m.group(1)):02d}:{m.group(2)}"
            if key == "couch_resolution":
                value = value.strip().lower().replace("×", "x").replace(" ", "")
                if value and not re.fullmatch(r"\d{3,5}x\d{3,5}", value):
                    raise ValueError("couch_resolution must look like 1920x1080")
            choices = cls._CHOICES.get(key)
            if choices:
                if key == "log_level":
                    value = value.upper()
                if value not in choices:
                    raise ValueError(f"{key} must be one of {', '.join(choices)}")
        _ = f
        return value


class SettingsStore:
    """Loads/saves :class:`Settings` and notifies listeners on change."""

    def __init__(self, db):
        self._db = db
        self._lock = threading.Lock()
        self._listeners: list[Callable[[str, Any], None]] = []
        self.settings = self._load()

    def _load(self) -> Settings:
        s = Settings()
        for row in self._db.query("SELECT key, value FROM settings"):
            if row["key"] not in Settings.field_names():
                continue
            try:
                value = Settings.coerce(row["key"], json.loads(row["value"]))
                if isinstance(value, dict):
                    value = {**getattr(s, row["key"]), **value}      # sources added in newer versions start on
                setattr(s, row["key"], value)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                log.warning("Ignoring invalid stored setting %s: %s", row["key"], exc)
        return s

    def get(self, key: str) -> Any:
        return getattr(self.settings, key)

    def set(self, key: str, value: Any) -> Any:
        value = Settings.coerce(key, value)
        with self._lock:
            old = getattr(self.settings, key)
            setattr(self.settings, key, value)
            with self._db.transaction() as c:
                c.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                          (key, json.dumps(value), time.time()))
        if old != value:
            for cb in list(self._listeners):
                try:
                    cb(key, value)
                except Exception:  # listeners must not break saving
                    log.exception("settings listener failed for %s", key)
        return value

    def update(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.set(k, v)

    def subscribe(self, cb: Callable[[str, Any], None]) -> None:
        self._listeners.append(cb)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self.settings).items()}

    def import_toml(self, path: Path) -> list[str]:
        """Apply a ``config.toml``; returns a list of warnings for rejected keys."""
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        flat: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict) and k != "sources_enabled":
                if k == "sources":
                    flat["sources_enabled"] = {sk: sv.get("enabled", True) if isinstance(sv, dict) else bool(sv)
                                               for sk, sv in v.items()}
                    continue
                for sk, sv in v.items():
                    flat[sk] = sv
            else:
                flat[k] = v
        warnings = []
        for k, v in flat.items():
            if k not in Settings.field_names():
                warnings.append(f"unknown setting '{k}' ignored")
                continue
            try:
                if k == "sources_enabled":
                    merged = dict(self.settings.sources_enabled)
                    merged.update(v)
                    v = merged
                self.set(k, v)
            except (ValueError, TypeError) as exc:
                warnings.append(f"{k}: {exc}")
        return warnings


def download_folder(settings: "Settings", title: str | None = None) -> str:
    """Where a download goes: the game's own folder in the installation folder (one folder for everything), or the
    separate download folder when that is switched off."""
    from ezgd.core.fsutil import safe_dirname
    if settings.downloads_in_game_folder:
        base = Path(settings.install_dir)
        return str(base / safe_dirname(title)) if title and title.strip() else str(base)
    return settings.download_dir
