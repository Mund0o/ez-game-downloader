"""Compatibility advisor: look at a game's files (and its own logs) and suggest concrete fixes.

Each suggestion carries the exact settings change it would make, so the UI can offer
an *Apply* button. Nothing is applied silently except where noted by the caller.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from glob import escape as glob_escape
from pathlib import Path

from ezgd.library.executables import release_launch_script
from ezgd.library.models import LibraryEntry
from ezgd.platform import IS_LINUX
from ezgd.platform.launcher import COMMAND_TOKEN, is_windows_binary
from ezgd.platform.proton import suggest_dll_overrides


@dataclass
class Suggestion:
    key: str
    title: str
    detail: str
    changes: dict = field(default_factory=dict)      # LibraryManager.update(**changes); {} = information only
    severity: str = "warning"                        # warning | info


def merged_overrides(current: str, add: list[str]) -> str:
    parts = [p for p in current.split(";") if p.strip()]
    names = {p.split("=", 1)[0].strip().lower() for p in parts}
    parts += [f"{d}=n,b" for d in add if d.lower() not in names]
    return ";".join(parts)


def _missing_overrides(entry: LibraryEntry) -> tuple[list[str], bool, str]:
    s = suggest_dll_overrides(Path(entry.executable))
    if "game_dlls" in entry.compat_config.fixes:
        return [], s.onlinefix, s.explanation              # the Fixes switch covers all of them
    have = {p.split("=", 1)[0].strip().lower() for p in entry.compat_config.dll_overrides.split(";") if p.strip()}
    return [d for d in s.found if d.lower() not in have], s.onlinefix, s.explanation


def _game_logs(prefix_dir: Path) -> list[Path]:
    roaming = prefix_dir / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming"
    if not roaming.is_dir():
        return []
    return sorted(roaming.glob("*/logs/godot*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:1]


def _log_text(prefix_dir: Path) -> str:
    out = ""
    for p in _game_logs(prefix_dir):
        try:
            text = p.read_text("utf-8", "replace")
        except OSError:
            continue
        # start-up problems are logged first, rendering errors keep repeating: keep both ends
        out += text[:300_000] + "\n" + text[-300_000:] if len(text) > 600_000 else text
    return out


def _steam_running() -> bool:
    if not shutil.which("pgrep"):
        return True
    try:
        return subprocess.run(["pgrep", "-x", "steam"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def with_game_args(options: str, extra: str) -> str:
    """Add arguments for the game itself, respecting %command% (Steam semantics)."""
    options = options.strip()
    if not options:
        return extra
    if COMMAND_TOKEN in options:
        return options.replace(COMMAND_TOKEN, f"{COMMAND_TOKEN} {extra}", 1)
    return f"{options} {extra}"


def suggestions(entry: LibraryEntry, prefix_dir: Path, game_extra: dict | None = None) -> list[Suggestion]:
    if not entry.executable or not Path(entry.executable).is_file():
        return []
    exe = Path(entry.executable)
    out: list[Suggestion] = []
    options = entry.launch_args or ""
    log = _log_text(prefix_dir) if IS_LINUX else ""
    windows_game = is_windows_binary(exe)

    # 1. launch options meant to run *before* the game but written without %command%
    if options.strip() and COMMAND_TOKEN not in options:
        try:
            first = shlex.split(options)[0]
        except (ValueError, IndexError):
            first = ""
        if first and not first.startswith(("-", "+", "/")) and shutil.which(first):
            fixed = f"{options.strip()}; {COMMAND_TOKEN}"
            out.append(Suggestion(
                "options-command", f"Run “{first}” before the game",
                f"Your launch options have no %command%, so — exactly as in Steam — they are added to the end of "
                f"the game's command line as arguments and “{first}” never runs. Change them to: {fixed}",
                {"launch_args": fixed}))

    # 2. the release starts this exe through its own script, which first swaps files the exe needs
    if exe.suffix.lower() not in (".cmd", ".bat"):
        found = release_launch_script(exe)
        if found:
            script, prep = found
            steam_restart = any(exe.parent.glob(f"{glob_escape(exe.name)}.*.STEAMSTART"))
            out.append(Suggestion(
                "launch-script", f"Start the game through {script.name}",
                f"This release starts {exe.name} with {script.name}, which first runs: " + " · ".join(prep[:4])
                + (" …" if len(prep) > 4 else "") + ". Started directly, the game runs with the wrong files"
                + (" — it already tried to restart through Steam (the .STEAMSTART files next to it), which is "
                   "what happens when the single-player steam_api.dll is in place" if steam_restart else "")
                + f". Makes {script.name} the executable (you confirm it on the next Play, like any new executable).",
                {"set_executable": str(script)}, "warning" if steam_restart else "info"))

    if IS_LINUX and windows_game:
        # 3. proxy DLLs the game ships (OnlineFix's winmm.dll, mod loaders) need native-first overrides
        missing, onlinefix, explanation = _missing_overrides(entry)
        if missing:
            evidence = ""
            if "No appID found" in log or "Steamworks initialization failed" in log:
                evidence = " The game's own log shows “Steamworks initialization failed: No appID found”, which " \
                           "is what happens when this DLL is not loaded."
            title = ("Load the OnlineFix fix (" if onlinefix else "Load the game's own ") + \
                ", ".join(f"{d}.dll" for d in missing) + (")" if onlinefix else "")
            out.append(Suggestion("dll-overrides", title, explanation + evidence, {"enable_fix": "game_dlls"}))

        # 4. Godot games that ship a Vulkan launcher: avoid Direct3D 12 → vkd3d-proton
        vulkan_bat = exe.parent / "launch_vulkan.bat"
        renderer_fixed = {"godot_vulkan", "godot_opengl"} & set(entry.compat_config.fixes)
        if vulkan_bat.is_file() and "--rendering-driver" not in options and not renderer_fixed:
            try:
                bat = vulkan_bat.read_text("utf-8", "replace")
            except OSError:
                bat = ""
            m = re.search(r"--rendering-driver\s+(vulkan)", bat)
            if m:
                d3d_errors = "rendering_device_driver_d3d12" in log and "ERR_CANT_CREATE" in log
                out.append(Suggestion(
                    "godot-vulkan", "Use Vulkan rendering",
                    ("The game's log shows Direct3D 12 swap-chain errors (ERR_CANT_CREATE). " if d3d_errors else "")
                    + "This Godot game starts with Direct3D 12, which Proton translates through vkd3d-proton; its "
                      "developers ship a Vulkan launcher (launch_vulkan.bat). Running Vulkan directly is usually "
                      "more reliable under Proton. Turns on the “Use Vulkan rendering” fix.",
                    {"enable_fix": "godot_vulkan"}, "warning" if d3d_errors else "info"))

        # 5. OnlineFix "play via Steam" needs the Steam client running (and signed in)
        if (game_extra or {}).get("network", "").lower().startswith("steam") and not _steam_running():
            out.append(Suggestion(
                "steam-running", "Start Steam before playing",
                "OnlineFix's page says this game plays online via Steam: start Steam and sign in first.",
                {}, "info"))
    return out
