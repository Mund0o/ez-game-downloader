"""Library entry model (downloaded / extracted / installed games)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ezgd.catalog.normalize import dedup_key


class LibraryStatus(StrEnum):
    DOWNLOADED = "downloaded"    # files on disk, nothing extracted yet
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"      # archive unpacked; user has not confirmed installation
    INSTALLED = "installed"      # user marked as installed (executable chosen)
    BROKEN = "broken"            # paths missing on disk


class LaunchMethod(StrEnum):
    AUTO = "auto"                # native on Windows; Proton (then Wine) for .exe on Linux
    NATIVE = "native"
    PROTON = "proton"
    WINE = "wine"
    STEAM = "steam"              # launch through a Steam non-Steam-game shortcut


@dataclass
class LibraryEntry:
    title: str
    status: LibraryStatus
    id: int | None = None
    game_id: str | None = None
    source: str | None = None
    download_id: int | None = None
    cover_url: str | None = None
    download_path: str | None = None
    install_path: str | None = None
    executable: str | None = None
    executable_trusted: bool = False
    launch_method: LaunchMethod = LaunchMethod.AUTO
    proton_version: str | None = None
    launch_args: str = ""
    steam_shortcut_appid: int | None = None
    notes: str | None = None
    added_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_played_at: float | None = None
    play_count: int = 0
    steam_app_id: int | None = None        # real Steam App ID → selects Proton per-game fixes
    compat: dict[str, Any] = field(default_factory=dict)   # see CompatConfig
    playtime_seconds: int = 0
    setup: dict[str, Any] = field(default_factory=dict)    # installer releases: see ezgd.library.repack
    release: dict[str, Any] = field(default_factory=dict)  # installed release + update check: see ezgd.library.updates
    organize: dict[str, Any] = field(default_factory=dict) # {"favorite": bool, "hidden": bool, "collections": [...]}

    COLUMNS = ("title", "dedup_key", "game_id", "source", "download_id", "cover_url", "download_path",
               "install_path", "executable", "executable_trusted", "launch_method", "proton_version",
               "launch_args", "status", "steam_shortcut_appid", "notes", "added_at", "updated_at",
               "last_played_at", "play_count", "steam_app_id", "compat", "playtime_seconds", "setup", "release",
               "organize")

    @property
    def compat_config(self) -> "CompatConfig":
        return CompatConfig.from_dict(self.compat)

    @property
    def dedup_key(self) -> str:
        return dedup_key(self.title)

    @property
    def favorite(self) -> bool:
        return bool((self.organize or {}).get("favorite"))

    @property
    def hidden(self) -> bool:
        return bool((self.organize or {}).get("hidden"))

    @property
    def collections(self) -> list[str]:
        return [str(c) for c in (self.organize or {}).get("collections") or []]

    def to_row(self) -> dict[str, Any]:
        row = {k: getattr(self, k) for k in self.COLUMNS}
        row["status"] = str(self.status)
        row["launch_method"] = str(self.launch_method)
        row["executable_trusted"] = int(self.executable_trusted)
        row["compat"] = json.dumps(self.compat or {})
        row["setup"] = json.dumps(self.setup or {})
        row["release"] = json.dumps(self.release or {})
        row["organize"] = json.dumps(self.organize or {})
        return row

    @classmethod
    def from_row(cls, row) -> "LibraryEntry":
        r = dict(row)
        return cls(
            id=r["id"], title=r["title"], status=LibraryStatus(r["status"]), game_id=r.get("game_id"),
            source=r.get("source"), download_id=r.get("download_id"), cover_url=r.get("cover_url"),
            download_path=r.get("download_path"), install_path=r.get("install_path"),
            executable=r.get("executable"), executable_trusted=bool(r.get("executable_trusted")),
            launch_method=LaunchMethod(r.get("launch_method") or "auto"),
            proton_version=r.get("proton_version"), launch_args=r.get("launch_args") or "",
            steam_shortcut_appid=r.get("steam_shortcut_appid"), notes=r.get("notes"),
            added_at=r["added_at"], updated_at=r["updated_at"], last_played_at=r.get("last_played_at"),
            play_count=r.get("play_count") or 0, steam_app_id=r.get("steam_app_id"),
            compat=_load_json(r.get("compat")), playtime_seconds=r.get("playtime_seconds") or 0,
            setup=_load_json(r.get("setup")), release=_load_json(r.get("release")),
            organize=_load_json(r.get("organize")),
        )


def _load_json(raw) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


@dataclass
class CompatConfig:
    """Per-game Proton settings (Linux)."""

    options: list[str] = field(default_factory=list)   # keys of ezgd.platform.proton.COMPAT_OPTIONS
    dll_overrides: str = ""            # WINEDLLOVERRIDES, e.g. "winmm=n,b;version=n,b"
    use_runtime: bool = True           # run inside the Steam Linux Runtime container, as Steam does
    protonfixes: bool = True           # let GE/CachyOS protonfixes apply per-game fixes
    report_app_id: bool = False        # also export SteamAppId/SteamGameId (Valve Proton per-app hacks)
    fixes: list[str] = field(default_factory=list)      # keys of ezgd.library.fixes (Fixes screen)
    fps_limit: int = 0                 # frames per second cap for DirectX games (DXVK / VKD3D); 0 = none

    @classmethod
    def from_dict(cls, d: dict | None) -> "CompatConfig":
        d = d or {}
        return cls(options=[str(x) for x in d.get("options", []) if isinstance(x, str)],
                   dll_overrides=str(d.get("dll_overrides", "")),
                   use_runtime=bool(d.get("use_runtime", True)),
                   protonfixes=bool(d.get("protonfixes", True)),
                   report_app_id=bool(d.get("report_app_id", False)),
                   fixes=[str(x) for x in d.get("fixes", []) if isinstance(x, str)],
                   fps_limit=max(0, min(1000, int(d.get("fps_limit") or 0))))

    def to_dict(self) -> dict:
        return {"options": self.options, "dll_overrides": self.dll_overrides, "use_runtime": self.use_runtime,
                "protonfixes": self.protonfixes, "report_app_id": self.report_app_id, "fixes": self.fixes,
                "fps_limit": self.fps_limit}
