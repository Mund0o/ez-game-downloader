"""Steam-emulator setup checks: which emulator a release uses, how it is configured, and what is missing.

Releases replace Steam's ``steam_api(64).dll`` with an emulator (Goldberg, CODEX/RUNE, ALI213, SmartSteamEmu,
OnlineFix …) or ship Valve's own DLL with a ``steam_appid.txt`` (DRM-free games). Most "Steam init failed" /
"restarts through Steam" problems are a missing or wrong piece of that setup, which can be put right:
writing ``steam_appid.txt``, filling in the App ID in the emulator's config, or putting the emulator files next to
the executable that actually loads them. Everything here only reads files; fixes are applied elsewhere, with
backups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

STEAM_DLLS = ("steam_api64.dll", "steam_api.dll")
# config files that belong with an emulator DLL (copied together when the DLL is in the wrong folder)
COMPANIONS = ("steam_emu.ini", "steam_api64.cdx", "steam_api.cdx", "steam_api64.rne", "steam_api.rne",
              "SteamConfig.ini", "SmartSteamEmu.ini", "cream_api.ini", "steam_appid.txt", "steam_interfaces.txt",
              "OnlineFix.ini", "OnlineFix64.dll", "OnlineFix.dll", "local_save.txt")
COMPANION_DIRS = ("steam_settings", "SmartSteamEmu")


@dataclass
class EmuSetup:
    kind: str                         # goldberg | codex | rune | ali213 | sse | creamapi | onlinefix | valve | unknown
    dll: Path                         # the steam_api DLL that belongs to it
    config: Path | None = None        # where its App ID is configured
    app_id: int | None = None         # the App ID it is configured with
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return {"goldberg": "Goldberg Steam Emulator", "codex": "CODEX Steam emulator", "rune": "RUNE Steam emulator",
                "ali213": "ALI213 Steam emulator", "sse": "SmartSteamEmu", "creamapi": "CreamAPI (DLC unlocker)",
                "onlinefix": "OnlineFix", "valve": "Valve's own Steam API (DRM-free release)",
                "unknown": "an unknown Steam emulator"}[self.kind]


def _strings(dll: Path, limit: int = 4 * 1024 * 1024) -> bytes:
    try:
        with open(dll, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def _ini_value(path: Path, keys: tuple[str, ...]) -> int | None:
    try:
        text = path.read_text("utf-8", "replace")
    except OSError:
        return None
    for k in keys:
        m = re.search(rf"^\s*{k}\s*=\s*(\d+)\s*$", text, re.I | re.M)
        if m:
            return int(m.group(1))
    return None


def _txt_id(path: Path) -> int | None:
    try:
        m = re.match(r"\s*(\d+)", path.read_text("utf-8", "replace"))
        return int(m.group(1)) if m else None
    except OSError:
        return None


def identify(dll: Path) -> EmuSetup:
    d = dll.parent
    blob = _strings(dll)
    if (d / "OnlineFix.ini").exists() or (d / "OnlineFix64.dll").exists():
        return EmuSetup("onlinefix", dll, d / "OnlineFix.ini", _ini_value(d / "OnlineFix.ini",
                                                                            ("RealAppId", "AppId", "FakeAppId")))
    if (d / "cream_api.ini").exists():
        return EmuSetup("creamapi", dll, d / "cream_api.ini", _ini_value(d / "cream_api.ini", ("appid",)))
    if (d / "steam_settings").is_dir() or b"Goldberg" in blob or b"gbe_" in blob or b"steam_settings" in blob:
        cfg = d / "steam_settings" / "steam_appid.txt"
        alt = d / "steam_appid.txt"
        return EmuSetup("goldberg", dll, cfg if cfg.exists() or not alt.exists() else alt,
                        _txt_id(cfg) if cfg.exists() else _txt_id(alt))
    if (d / "steam_emu.ini").exists():
        kind = "rune" if list(d.glob("*.rne")) or b"RUNE" in blob else "codex"
        return EmuSetup(kind, dll, d / "steam_emu.ini", _ini_value(d / "steam_emu.ini", ("AppId",)))
    if (d / "SteamConfig.ini").exists():
        return EmuSetup("ali213", dll, d / "SteamConfig.ini", _ini_value(d / "SteamConfig.ini", ("AppID", "AppId")))
    if (d / "SmartSteamEmu.ini").exists():
        return EmuSetup("sse", dll, d / "SmartSteamEmu.ini", _ini_value(d / "SmartSteamEmu.ini", ("AppId",)))
    if b"SteamAPI_RestartAppIfNecessary" in blob and (b"Valve" in blob or b"valvesoftware" in blob.lower()) and \
            b"steam_emu" not in blob.lower():
        return EmuSetup("valve", dll, d / "steam_appid.txt", _txt_id(d / "steam_appid.txt"))
    return EmuSetup("unknown", dll)


def steam_dll_for(exe: Path, game_root: Path | None = None) -> tuple[Path | None, list[Path]]:
    """(the steam_api DLL next to the exe, other copies elsewhere in the game folder). Only the game's own folder is
    searched: walking up from Game/Game.exe used to reach the home folder (every other game's steam_api64.dll — slow,
    and a wrong copy could be offered)."""
    here = next((exe.parent / n for n in STEAM_DLLS if (exe.parent / n).exists()), None)
    root = Path(game_root) if game_root else exe.parent
    if game_root is None:
        for _ in range(3):                     # e.g. Game/Binaries/Win64/Game.exe → Game/ (Unreal layout only)
            if root.name.lower() not in ("win64", "win32", "binaries", "bin", "x64", "x86"):
                break
            root = root.parent
    if game_root is not None and exe.parent != root and root not in exe.parents:
        root = exe.parent
    others = []
    for n in STEAM_DLLS:
        for p in list(root.glob(f"**/{n}"))[:20]:
            if p.parent != exe.parent and ".ezgd-backup-" not in str(p):
                others.append(p)
    return here, others


def needs_steam_dll(exe: Path) -> str | None:
    """Which steam_api DLL the exe imports (from its import table), if any."""
    from ezgd.platform.pe import read_pe
    info = read_pe(exe)
    if info is None:
        return None
    for n in info.imports + info.delay_imports:
        if n in STEAM_DLLS:
            return n
    return None
