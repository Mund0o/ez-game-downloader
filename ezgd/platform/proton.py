"""Running Windows games with Proton the way Steam does.

Steam launches a Proton game as

    <Steam Linux Runtime>/_v2-entry-point --verb=waitforexitandrun -- \\
        <Proton>/proton waitforexitandrun <game.exe> <args>

where the runtime is whichever tool the Proton build names in its
``toolmanifest.vdf`` (``require_tool_appid``), resolved through Steam's
``appmanifest_<id>.acf``. Each game gets its own prefix
(``STEAM_COMPAT_DATA_PATH``).

Per-game fixes:
* GE-Proton and Proton-CachyOS ship umu-protonfixes. It looks up the game id
  from ``UMU_ID`` first, then ``SteamAppId``/``SteamGameId``, and finally the
  last number in ``STEAM_COMPAT_DATA_PATH``. We therefore always set ``UMU_ID``:
  the numeric Steam App ID when known (→ ``gamefixes-steam/<id>.py``), else
  ``umu-0`` (default fixes only) — never letting it guess from our prefix path.
* ``UMU_ID`` also tells GE-Proton "not a Steam game": since GE-Proton11-7 its ``proton`` script then switches
  Wine's bridge to the Steam client off (``lsteamclient=d``) unless ``UMU_USE_STEAM=1`` — a game that needs the
  running Steam client (OnlineFix's multiplayer fix) failed with GE in the app while it worked with GE in Steam
  (which never sets ``UMU_ID``) and with Valve's Proton (no UMU logic). Builds up to 11-6 (and Proton-CachyOS
  11.0) had that switch the other way round. :func:`umu_steam_mode` reads which way a build works, so such games
  keep the Steam client in every build (see ``steam_client`` in :func:`proton_command`).
* Valve Proton keys its built-in per-app tweaks on ``SteamAppId``/``SteamGameId``.
  Exporting those is opt-in because games whose Steam emulation reads
  ``SteamAppId`` (e.g. OnlineFix's online fix) may misbehave.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ezgd.library.models import CompatConfig
from ezgd.platform.steam import ProtonInstall, find_runtime


@dataclass(frozen=True)
class CompatOption:
    key: str
    env: str
    value: str
    label: str
    help: str


# Documented Proton runtime options (Proton README "Runtime Config Options") + DXVK HUD.
COMPAT_OPTIONS: list[CompatOption] = [
    CompatOption("wined3d", "PROTON_USE_WINED3D", "1", "Use WineD3D (OpenGL) instead of DXVK/VKD3D",
                 "Helps very old games or GPUs without good Vulkan support."),
    CompatOption("no_esync", "PROTON_NO_ESYNC", "1", "Disable esync",
                 "Try when a game hangs or crashes at start."),
    CompatOption("no_fsync", "PROTON_NO_FSYNC", "1", "Disable fsync",
                 "Try when a game hangs or crashes at start."),
    # (PROTON_ENABLE_NVAPI is gone from current Proton builds — NVAPI is on by default on NVIDIA; checked
    # 2026-10-13 in Proton Experimental / 11.0, GE-Proton11 and Proton-CachyOS: PROTON_FORCE_/DISABLE_NVAPI)
    CompatOption("nvapi", "PROTON_FORCE_NVAPI", "1", "Force NVAPI on (DLSS / Reflex)",
                 "NVAPI is already on for NVIDIA GPUs; this forces it where Proton would leave it off."),
    CompatOption("no_nvapi", "PROTON_DISABLE_NVAPI", "1", "Turn NVAPI off (DLSS / Reflex)",
                 "For games that crash in NVIDIA's DLSS/Reflex code under Proton."),
    CompatOption("heap_delay_free", "PROTON_HEAP_DELAY_FREE", "1", "Delay freeing memory (heap workaround)",
                 "Works around games that use memory after freeing it (crashes Windows happens to tolerate)."),
    CompatOption("dxvk_d3d8", "PROTON_DXVK_D3D8", "1", "DirectX 8 through DXVK",
                 "DirectX 8 games normally use WineD3D; DXVK is often faster and draws some of them correctly."),
    CompatOption("no_d3d11", "PROTON_NO_D3D11", "1", "Turn off DirectX 11 (use the DirectX 10 fallback)",
                 "Only for games that can fall back to DirectX 10 and run better with it."),
    CompatOption("wow64", "PROTON_USE_WOW64", "1", "New 32-bit mode (WoW64)",
                 "Runs 32-bit games through Wine's new WoW64 mode; helps some 32-bit games that crash."),
    CompatOption("game_drive", "PROTON_SET_GAME_DRIVE", "1", "Give the game's folder a drive letter (S:)",
                 "For games that break on long folder paths or need their own drive."),
    CompatOption("prefer_sdl", "PROTON_PREFER_SDL", "1", "Controllers through SDL",
                 "Fixes controllers that are not seen, or seen twice (GE / CachyOS builds)."),
    CompatOption("no_steaminput", "PROTON_NO_STEAMINPUT", "1", "Without Steam Input",
                 "For controllers that act strangely when Steam Input translates them (GE / CachyOS builds)."),
    CompatOption("no_hidraw", "PROTON_DISABLE_HIDRAW", "1", "Controllers without raw HID access",
                 "Fixes double input or wrong buttons on some controllers."),
    CompatOption("no_ntsync", "PROTON_NO_NTSYNC", "1", "Disable ntsync",
                 "Falls back to fsync; for games that hang with the kernel's new Wine sync (GE / CachyOS builds)."),
    CompatOption("hide_nvidia", "PROTON_HIDE_NVIDIA_GPU", "1", "Report an NVIDIA GPU as AMD",
                 "Works around games that refuse to start on NVIDIA under Proton."),
    CompatOption("wayland", "PROTON_ENABLE_WAYLAND", "1", "Native Wayland (experimental)",
                 "Proton 10+; may improve latency/HDR on Wayland desktops."),
    CompatOption("laa", "PROTON_FORCE_LARGE_ADDRESS_AWARE", "1", "Force large-address-aware",
                 "Lets 32-bit games use more than 2 GB of memory."),
    CompatOption("old_gl", "PROTON_OLD_GL_STRING", "1", "Limit the OpenGL extension string",
                 "Fixes crashes in some very old OpenGL games."),
    CompatOption("log", "PROTON_LOG", "1", "Write a Proton debug log",
                 "Saved in the app's log folder as steam-<id>.log."),
    CompatOption("dxvk_hud", "DXVK_HUD", "fps", "Show an FPS counter (DXVK HUD)",
                 "Overlay drawn by DXVK in Direct3D games."),
]
OPTIONS_BY_KEY = {o.key: o for o in COMPAT_OPTIONS}

_SUPPORT_CACHE: dict[str, tuple[float, bytes]] = {}


def proton_supports(proton_dir, var: str) -> bool | None:
    """Does this Proton build read ``var``? From its own ``proton`` script (and wineserver, which reads
    PROTON_NO_NTSYNC in Proton-CachyOS). None when it cannot be told; variables that are not Proton's own (DXVK_*,
    VKD3D_*, WINE_*) count as supported."""
    if not var.startswith("PROTON_"):
        return True
    if not proton_dir:
        return None
    root = Path(proton_dir)
    key = str(root)
    try:
        mtime = (root / "proton").stat().st_mtime
    except OSError:
        return None
    hit = _SUPPORT_CACHE.get(key)
    if hit is None or hit[0] != mtime:
        blob = b""
        for f in (root / "proton", root / "files" / "bin" / "wineserver"):
            try:
                blob += f.read_bytes()[:64 * 1024 * 1024]
            except OSError:
                continue
        hit = _SUPPORT_CACHE[key] = (mtime, blob)
    return var.encode() in hit[1]

_UMU_MODE_CACHE: dict[str, tuple[float, str]] = {}


def umu_steam_mode(proton_dir) -> str:
    """How this Proton build treats the Steam client when ``UMU_ID`` is set — from its own ``proton`` script:

    * ``"flag"``: Wine's Steam client bridge is switched off unless ``UMU_USE_STEAM=1`` (GE-Proton11-7 and later)
    * ``"inverted"``: it is switched off only *with* ``UMU_USE_STEAM=1`` (GE-Proton up to 11-6, Proton-CachyOS 11.0)
    * ``"none"``: the bridge is never switched off (Valve's Proton: no UMU handling)
    * ``"unknown"``: something else — the safe answer is a launch without ``UMU_ID``, as Steam does it
    """
    if not proton_dir:
        return "unknown"
    script = Path(proton_dir) / "proton"
    try:
        mtime = script.stat().st_mtime
    except OSError:
        return "unknown"
    hit = _UMU_MODE_CACHE.get(str(script))
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        text = script.read_text("utf-8", "replace")
    except OSError:
        return "unknown"
    mode = _umu_mode_of(text)
    _UMU_MODE_CACHE[str(script)] = (mtime, mode)
    return mode


def _umu_mode_of(text: str) -> str:
    lines = text.splitlines()
    hits = [k for k, line in enumerate(lines) if "lsteamclient=d" in line]
    if not hits:
        return "none"
    modes = set()
    for k in hits:
        indent = len(lines[k]) - len(lines[k].lstrip())
        guard = ""
        for j in range(k - 1, max(-1, k - 12), -1):            # the condition the line sits under
            line = lines[j]
            if line.strip() and len(line) - len(line.lstrip()) < indent:
                guard = line.strip()
                break
        names = re.findall(r"\b([a-z_][a-z0-9_]*)\b", guard)
        defined = " ".join(l for l in lines[max(0, k - 40):k] if any(re.match(rf"\s*{re.escape(n)}\s*=[^=]", l)
                                                                     for n in names if n not in ("if", "in", "and")))
        cond = guard + " " + defined
        if "UMU_USE_STEAM" in cond and re.search(r"UMU_USE_STEAM[\"']\)\s*!=\s*[\"']1", cond):
            modes.add("flag")                   # off unless UMU_USE_STEAM=1
        elif re.fullmatch(r"if\s+[\"']UMU_ID[\"']\s+in\s+os\.environ\s*:", guard) and \
                re.search(r"UMU_USE_STEAM[\"'],\s*[\"']0[\"']\)\s*!=\s*[\"']1", "\n".join(lines[max(0, k - 40):k])):
            modes.add("inverted")               # in the branch taken with UMU_USE_STEAM=1
        else:
            modes.add("unknown")
    return modes.pop() if len(modes) == 1 else "unknown"


# Wine has built-in versions of these DLLs, so a copy shipped next to a game's
# .exe (proxy/hook DLLs used by mods, launchers and fixes) is ignored unless the
# DLL is overridden to "native, then builtin".
PROXY_DLLS = ("winmm", "winhttp", "version", "dinput8", "dsound", "wininet", "iphlpapi", "xinput1_3",
              "xinput1_4", "xinput9_1_0", "dbghelp", "winhttp",
              "dwmapi")                                 # UE4SS, the Unreal mod loader (Expedition 33's co-op mod)
ONLINEFIX_MARKERS = ("onlinefix64.dll", "onlinefix.dll", "onlinefix.ini")


@dataclass
class OverrideSuggestion:
    overrides: str
    found: list[str]
    onlinefix: bool

    @property
    def explanation(self) -> str:
        if not self.found:
            return "No DLLs that need overriding were found next to the executable."
        names = ", ".join(f"{n}.dll" for n in self.found)
        text = (f"The game ships its own {names}. Wine would use its built-in versions instead, so these are "
                f"set to load the game's copy first (native, then built-in).")
        if self.onlinefix:
            text += " The OnlineFix multiplayer fix was detected in the game folder."
        return text


def game_binary_dirs(exe: Path) -> list[Path]:
    """Where the game's own DLLs are: next to the exe — and, for an Unreal game started through its small launcher
    (Expedition33_Steam.exe), next to the real game, <Project>/Binaries/Win64 (WinGDK on Game Pass). Expedition 33's
    OnlineFix and UE4SS loaders sat there and were never found, so Wine's own winmm/dwmapi replaced them."""
    exe = Path(exe)
    dirs = [exe.parent]
    root = exe.parent
    if (root / "Engine").is_dir():
        for ship in sorted(root.glob("*/Binaries/Win*/*-Shipping.exe"))[:2]:
            if ship.parent not in dirs:
                dirs.append(ship.parent)
    return dirs


def suggest_dll_overrides(exe: Path) -> OverrideSuggestion:
    present = {}
    for folder in reversed(game_binary_dirs(exe)):
        if folder.is_dir():
            present.update({p.name.lower(): p for p in folder.glob("*") if p.is_file()})
    found = sorted({Path(n).stem for n in present if n.endswith(".dll") and Path(n).stem in PROXY_DLLS})
    onlinefix = any(m in present for m in ONLINEFIX_MARKERS)
    return OverrideSuggestion(";".join(f"{d}=n,b" for d in found), found, onlinefix)


_OVERRIDE_RE = re.compile(r"^[A-Za-z0-9_.\-*,]+=[nbd,]*$")


def validate_overrides(text: str) -> str:
    """Normalise a WINEDLLOVERRIDES string; raises ValueError on garbage."""
    parts = [p.strip() for p in text.replace(" ", "").split(";") if p.strip()]
    for p in parts:
        if not _OVERRIDE_RE.match(p):
            raise ValueError(f"invalid DLL override '{p}' (expected e.g. winmm=n,b)")
    return ";".join(parts)


@dataclass
class ProtonCommand:
    argv: list[str]
    env: dict[str, str]
    description: str
    runtime_name: str | None


def proton_command(exe: Path, proton: ProtonInstall, *, steam_root: Path, prefix_dir: Path,
                   game_root: Path | None = None, steam_app_id: int | None = None,
                   compat: CompatConfig | None = None, log_dir: Path | None = None,
                   verb: str = "waitforexitandrun", steam_client: bool = False) -> ProtonCommand:
    """``steam_client``: the game needs the running Steam client (OnlineFix): Wine's bridge to it stays on, in
    every build (see umu_steam_mode)."""
    compat = compat or CompatConfig()
    game_root = game_root or exe.parent
    env = {
        "STEAM_COMPAT_DATA_PATH": str(prefix_dir),
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam_root),
        "STEAM_COMPAT_INSTALL_PATH": str(game_root),
        "UMU_ID": str(steam_app_id) if steam_app_id else "umu-0",
    }
    # Never export SteamAppId/SteamGameId unless asked: the Steam API reads SteamAppId before anything
    # else, so even "0" breaks games ("No appID found") — e.g. OnlineFix's fix, which uses its own id.
    # Proton itself copes without them.
    if steam_app_id and compat.report_app_id:
        env["SteamAppId"] = env["SteamGameId"] = str(steam_app_id)
    if not compat.protonfixes:
        env["PROTONFIXES_DISABLE"] = "1"
    steam_mode = umu_steam_mode(proton.path) if steam_client else ""
    if steam_mode == "flag":
        env["UMU_USE_STEAM"] = "1"            # GE-Proton11-7+: keep lsteamclient (and start through steam.exe)
    elif steam_mode == "unknown":
        del env["UMU_ID"]                      # as Steam starts it: nothing switches the Steam client off
    for key in compat.options:
        opt = OPTIONS_BY_KEY.get(key)
        if opt:
            env[opt.env] = opt.value
    if "PROTON_LOG" in env and log_dir:
        env["PROTON_LOG_DIR"] = str(log_dir)
    if compat.dll_overrides:
        env["WINEDLLOVERRIDES"] = validate_overrides(compat.dll_overrides)

    proton_cmd = [str(proton.script), verb]
    runtime = find_runtime(steam_root, proton.require_tool_appid) if compat.use_runtime else None
    tool_paths = [str(proton.path)]
    from ezgd.library.dxlayers import base_of
    base = base_of(proton.path)                     # a Proton variant: its links lead into the Proton it mirrors
    if base is not None:
        tool_paths.append(str(base))
    if runtime is not None:
        tool_paths.append(str(runtime.path))
        # pressure-vessel shares $HOME; make sure games/prefixes elsewhere are visible too
        env["PRESSURE_VESSEL_FILESYSTEMS_RW"] = ":".join(dict.fromkeys([str(game_root), str(prefix_dir)]))
        argv = runtime.command(verb) + proton_cmd + [str(exe)]
    else:
        argv = proton_cmd + [str(exe)]
    env["STEAM_COMPAT_TOOL_PATHS"] = ":".join(tool_paths)

    parts = [proton.name]
    if runtime is not None:
        parts.append(runtime.name)
    elif compat.use_runtime and proton.require_tool_appid:
        parts.append("Steam Linux Runtime not installed — running Proton directly")
    if proton.has_protonfixes and compat.protonfixes:
        parts.append(f"protonfixes for app {steam_app_id}" if steam_app_id else "protonfixes (defaults)")
    if steam_client:
        parts.append("Steam client on" + {"flag": " (UMU_USE_STEAM)", "unknown": " (no UMU_ID)"}.get(steam_mode, ""))
    return ProtonCommand(argv, env, " · ".join(parts), runtime.name if runtime else None)


def umu_command(exe: Path, umu_run: str, *, prefix_dir: Path, proton: ProtonInstall | None = None,
                steam_app_id: int | None = None, compat: CompatConfig | None = None,
                steam_client: bool = False) -> ProtonCommand:
    """umu-launcher: Proton inside the Steam Runtime without Steam (umu downloads what it needs).

    Documented variables: WINEPREFIX, GAMEID, STORE, PROTONPATH ("GE-Proton" = latest GE-Proton).
    A numeric GAMEID reaches protonfixes as the Steam App ID (gamefixes-steam/<id>.py).
    """
    compat = compat or CompatConfig()
    env = {"WINEPREFIX": str(prefix_dir), "GAMEID": str(steam_app_id) if steam_app_id else "umu-default",
           "STORE": "none", "PROTONPATH": str(proton.path) if proton else "GE-Proton"}
    if not compat.protonfixes:
        env["PROTONFIXES_DISABLE"] = "1"
    if steam_client and (proton is None or umu_steam_mode(proton.path) == "flag"):
        env["UMU_USE_STEAM"] = "1"            # (umu always sets UMU_ID; the latest GE needs this for Steam)
    for key in compat.options:
        opt = OPTIONS_BY_KEY.get(key)
        if opt:
            env[opt.env] = opt.value
    if compat.dll_overrides:
        env["WINEDLLOVERRIDES"] = validate_overrides(compat.dll_overrides)
    parts = ["umu-launcher", proton.name if proton else "latest GE-Proton (downloaded on first launch)",
             "Steam Runtime"]
    if compat.protonfixes:
        parts.append(f"protonfixes for app {steam_app_id}" if steam_app_id else "protonfixes (defaults)")
    if steam_client:
        parts.append("Steam client on" + (" (UMU_USE_STEAM)" if env.get("UMU_USE_STEAM") else ""))
    return ProtonCommand([umu_run, str(exe)], env, " · ".join(parts), "Steam Runtime (umu)")


def prefix_ready(prefix_dir: Path) -> bool:
    # Steam layout keeps the Wine prefix in <compatdata>/pfx; umu-launcher uses the folder itself
    return (prefix_dir / "pfx" / "system.reg").is_file() or (prefix_dir / "system.reg").is_file()


# Proton prefixes say the .NET Framework is installed (Wine Mono stands in for it: NDP v4 "Release" 4.8.1 on the
# 32-bit side), so Microsoft's .NET installers stop at "Same or higher version of .NET Framework 4 has already been
# installed" and report success without installing anything. winetricks removes those keys only after uninstalling
# Wine Mono — which Proton ships without an uninstaller entry, so it never does.
_NDP_KEYS = [rf"HKLM\Software\{side}Microsoft\NET Framework Setup\NDP\{v}"
             for side in ("", "Wow6432Node\\") for v in ("v3.5", "v4")]


def winetricks_command(winetricks: str, verbs: list[str], prefix_dir: Path) -> list[str]:
    """The command that runs winetricks with ``verbs`` (validated by the caller) in a Proton prefix.

    First, links in the prefix to Proton files that do not exist are removed: Proton-CachyOS prefixes link 34 files
    (dxva2.dll, evr.dll, uiautomationcore.dll, Framework/v4.0.30319/ngen.exe …) to builtins it does not ship, and
    Microsoft's installers cannot write over them (.NET 4: "compressed file wasn't installed (Dxva2_amd64.dll)",
    error 1603; winetricks then said ngen.exe was missing). For .NET Framework verbs the prefix's claim to have it is
    cleared too (see _NDP_KEYS)."""
    remove_dead_links(prefix_dir / "pfx" / "drive_c" / "windows")
    if not any(v.startswith("dotnet") for v in verbs):
        return [winetricks, "-q", *verbs]
    script = "".join(f'"$WINE" reg delete "{k}" /f >/dev/null 2>&1; ' for k in _NDP_KEYS)
    return ["/bin/sh", "-c", script + 'exec "$@"', "winetricks", winetricks, "-q", *verbs]


def remove_dead_links(folder: Path) -> int:
    """Remove symbolic links under ``folder`` whose target does not exist (nothing else is touched)."""
    n = 0
    if folder.is_dir():
        for link in folder.rglob("*"):
            if link.is_symlink() and not link.exists():
                try:
                    link.unlink()
                    n += 1
                except OSError:
                    pass
    return n


def winetricks_env(proton: ProtonInstall, prefix_dir: Path) -> dict[str, str]:
    """Environment for running winetricks/winecfg against a Proton prefix on the host."""
    wine = proton.wine_bin
    if wine is None:
        raise FileNotFoundError(f"{proton.name} has no wine binary")
    return {"WINEPREFIX": str(prefix_dir / "pfx"), "WINE": str(wine),
            "WINESERVER": str(wine.with_name("wineserver")),
            "PATH": f"{wine.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
