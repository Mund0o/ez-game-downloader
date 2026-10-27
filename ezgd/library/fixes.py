"""Catalog of per-game fixes that can be switched on and off (the *Fixes* screen).

A fix is an on/off switch stored with the game; at launch the enabled fixes are
turned into environment variables, wrapper programs and game arguments. They are
kept separate from the user's own launch options, so unticking a fix undoes it
completely. ``recommended`` explains why a fix was suggested for this particular
game (from its files and its own logs).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ezgd.library.models import CompatConfig, LibraryEntry
from ezgd.platform import IS_LINUX
from ezgd.platform.launcher import is_windows_binary
from ezgd.platform.proton import COMPAT_OPTIONS, suggest_dll_overrides

CATEGORIES = ["Recommended", "Graphics", "Audio", "Stability", "Performance & overlays", "Compatibility",
              "Troubleshooting"]
OPTION_CATEGORY = {"wined3d": "Graphics", "nvapi": "Graphics", "hide_nvidia": "Graphics", "wayland": "Graphics",
                   "old_gl": "Graphics", "no_esync": "Stability", "no_fsync": "Stability", "laa": "Stability",
                   "dxvk_hud": "Performance & overlays", "log": "Troubleshooting", "no_nvapi": "Graphics",
                   "heap_delay_free": "Stability", "dxvk_d3d8": "Graphics", "no_d3d11": "Graphics",
                   "wow64": "Compatibility", "game_drive": "Compatibility", "prefer_sdl": "Compatibility",
                   "no_steaminput": "Compatibility", "no_hidraw": "Compatibility", "no_ntsync": "Stability"}


@dataclass
class GameContext:
    entry: LibraryEntry
    exe: Path | None
    log: str                       # the game's own log text (may be empty)
    linux: bool = IS_LINUX

    @property
    def folder(self) -> Path | None:
        return self.exe.parent if self.exe else None

    @property
    def windows_game(self) -> bool:
        return bool(self.exe and is_windows_binary(self.exe))

    @property
    def proton_game(self) -> bool:
        return self.linux and self.windows_game

    def has(self, name: str) -> bool:
        return bool(self.folder and (self.folder / name).is_file())

    @property
    def godot(self) -> bool:
        if not self.folder:
            return False
        return self.has("launch_vulkan.bat") or self.has("launch_opengl.bat") or \
            any(self.folder.glob("*.pck")) or "godotengine.org" in self.log[:2000]


@dataclass
class FixState:
    key: str
    category: str
    title: str
    description: str
    enabled: bool
    recommended: str | None = None      # reason, when suggested for this game
    available: bool = True
    unavailable_reason: str | None = None
    group: str | None = None            # fixes in the same group are mutually exclusive

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _dlls(ctx: GameContext) -> list[str]:
    return suggest_dll_overrides(ctx.exe).found if ctx.exe else []


def _cfg(ctx: GameContext) -> CompatConfig:
    return ctx.entry.compat_config


def _d3d12_errors(ctx: GameContext) -> bool:
    return "rendering_device_driver_d3d12" in ctx.log and "ERR_CANT_CREATE" in ctx.log


def catalog(ctx: GameContext) -> list[FixState]:
    """Every fix that applies to this game, with its current state."""
    cfg = _cfg(ctx)
    on = set(cfg.fixes)
    out: list[FixState] = []

    if ctx.proton_game:
        dlls = _dlls(ctx)
        if dlls:
            onlinefix = suggest_dll_overrides(ctx.exe).onlinefix
            covered = {p.split("=", 1)[0].strip().lower() for p in cfg.dll_overrides.split(";") if p.strip()}
            reason = None
            if onlinefix:
                reason = "OnlineFix's fix is in the game folder; it is loaded through " + \
                         ", ".join(f"{d}.dll" for d in dlls) + " and does not work without this."
            if "No appID found" in ctx.log:
                reason = (reason or "") + " The game's log shows “Steamworks initialization failed: No appID found”."
            out.append(FixState(
                "game_dlls", "Compatibility", "Load the game's own DLLs (" + ", ".join(f"{d}.dll" for d in dlls) + ")",
                "Wine normally replaces these with its own built-in versions, which silently disables mods, "
                "loaders and OnlineFix's multiplayer fix. This makes Wine use the game's copies first.",
                "game_dlls" in on or all(d.lower() in covered for d in dlls), (reason or "").strip() or None))

    if ctx.godot:
        vulkan_reason = None
        if _d3d12_errors(ctx):
            vulkan_reason = "The game's log shows Direct3D 12 swap-chain errors (black screen)."
        elif ctx.proton_game and ctx.has("launch_vulkan.bat"):
            vulkan_reason = "The developers ship a Vulkan launcher; Vulkan is usually more reliable under Proton."
        out.append(FixState(
            "godot_vulkan", "Graphics", "Use Vulkan rendering (--rendering-driver vulkan)",
            "Fixes black screens and graphics glitches in Godot games that default to Direct3D 12, which Proton "
            "has to translate. Same as the game's launch_vulkan.bat.",
            "godot_vulkan" in on, vulkan_reason, group="godot_renderer"))
        out.append(FixState(
            "godot_opengl", "Graphics", "Use OpenGL rendering (--rendering-driver opengl3)",
            "Fallback for very old or unusual GPUs where Vulkan does not work. Lower quality, but most compatible.",
            "godot_opengl" in on, None, group="godot_renderer"))

    if ctx.proton_game:
        for opt in COMPAT_OPTIONS:
            out.append(FixState(f"opt:{opt.key}", OPTION_CATEGORY.get(opt.key, "Compatibility"), opt.label,
                                f"{opt.help} ({opt.env}={opt.value})", opt.key in cfg.options))
        out.append(FixState(
            "protonfixes", "Compatibility", "Apply protonfixes (per-game fixes from GE-Proton / Proton-CachyOS)",
            "Community fixes keyed by the game's Steam App ID. Untick only if a fix causes problems.",
            cfg.protonfixes))
        out.append(FixState(
            "runtime", "Compatibility", "Run inside the Steam Linux Runtime (what Steam does)",
            "Uses the same container Steam uses for Proton. Untick only to troubleshoot.", cfg.use_runtime))
        out.append(FixState(
            "report_app_id", "Troubleshooting", "Tell Proton the Steam App ID (SteamAppId)",
            "Enables Valve Proton's built-in per-game tweaks. Can break games whose Steam emulation reads "
            "SteamAppId, such as OnlineFix's fix — leave off for OnlineFix games.", cfg.report_app_id))

    out.append(FixState(
        "pulse_latency", "Audio", "Fix crackling or stuttering sound (PULSE_LATENCY_MSEC=60)",
        "Gives the game a larger audio buffer. Only affects this game — other apps such as TeamSpeak keep "
        "working normally.", "pulse_latency" in on))
    out.append(_tool_fix(ctx, "gamemode", "gamemoderun", "Performance & overlays",
                         "Run with GameMode (gamemoderun)",
                         "Asks the system for maximum CPU/GPU performance while the game runs."))
    out.append(_tool_fix(ctx, "mangohud", "mangohud", "Performance & overlays", "Show MangoHud overlay",
                         "FPS, frame-time and temperature overlay."))
    from ezgd.platform import IS_LINUX
    if IS_LINUX:
        overlay_crash = "gameoverlayrenderer" in ctx.log and any(
            w in ctx.log for w in ("Segmentation fault", "SIGSEGV", "backtrace", "Unhandled exception"))
        from ezgd.platform.machine import performance_cpus
        pcores = performance_cpus()
        if pcores and ctx.windows_game:
            out.append(FixState(
                "pcores", "Stability", f"Run on the performance cores only (CPUs {pcores})",
                "This CPU has fast performance cores and slower efficiency cores, and Linux moves the game's threads "
                "between them. Releases that emulate Denuvo (EMPRESS) give up at a random moment when that happens "
                "and the game quits; keeping the game on the performance cores avoids it.", "pcores" in on,
                "EMPRESS's EMP.dll is in the game's folder: its Denuvo emulation quits the game at random on this "
                "CPU's mix of cores." if ctx.has("EMP.dll") else None, bool(shutil.which("taskset")),
                None if shutil.which("taskset") else "taskset (util-linux) is not installed."))
        out.append(FixState(
            "no_steam_overlay", "Performance & overlays", "Turn off the Steam overlay for this game",
            "The Steam overlay (Shift+Tab) is loaded into games while Steam is running. A few games crash or "
            "stutter with it; this keeps it out of this game only.", "no_steam_overlay" in on,
            "The game's last crash mentions the Steam overlay (gameoverlayrenderer)." if overlay_crash else None))
    return out


def _tool_fix(ctx: GameContext, key: str, tool: str, category: str, title: str, desc: str) -> FixState:
    installed = bool(shutil.which(tool))
    return FixState(key, category, title, desc, key in set(_cfg(ctx).fixes), None, installed,
                    None if installed else f"{tool} is not installed — install it from Settings → System check.")


def apply_selection(ctx: GameContext, enabled: set[str]) -> CompatConfig:
    """Turn the ticked checkboxes into the game's stored compatibility config."""
    cfg = _cfg(ctx)
    states = {s.key: s for s in catalog(ctx)}
    known = set(states)
    # mutually exclusive groups: keep the first ticked one
    seen_groups: set[str] = set()
    for key in [k for k in states if k in enabled]:
        g = states[key].group
        if g:
            if g in seen_groups:
                enabled.discard(key)
            seen_groups.add(g)
    cfg.options = [o.key for o in COMPAT_OPTIONS if f"opt:{o.key}" in enabled] + \
                  [o for o in cfg.options if f"opt:{o}" not in known]          # keep options not shown here
    for flag, attr in (("protonfixes", "protonfixes"), ("runtime", "use_runtime"), ("report_app_id", "report_app_id")):
        if flag in known:
            setattr(cfg, attr, flag in enabled)
    plain = {"game_dlls", "godot_vulkan", "godot_opengl", "pulse_latency", "gamemode", "mangohud", "no_steam_overlay",
             "pcores"}
    cfg.fixes = sorted(k for k in plain if k in enabled and k in known) + \
        [k for k in cfg.fixes if k not in known]                               # keep fixes not shown here
    if "game_dlls" in known and "game_dlls" not in enabled and ctx.exe:
        # also remove the same DLLs if they were added to the manual override field earlier
        drop = {d.lower() for d in _dlls(ctx)}
        cfg.dll_overrides = ";".join(p for p in cfg.dll_overrides.split(";")
                                     if p.strip() and p.split("=", 1)[0].strip().lower() not in drop)
    return cfg


@dataclass
class FixEffects:
    env: dict
    wrappers: list[str]
    args: list[str]
    notes: list[str]


def effects(ctx: GameContext) -> FixEffects:
    """What the enabled fixes add to the launch command."""
    cfg = _cfg(ctx)
    env: dict = {}
    wrappers: list[str] = []
    args: list[str] = []
    notes: list[str] = []
    on = set(cfg.fixes)
    if "game_dlls" in on and ctx.proton_game:
        dlls = _dlls(ctx)
        if dlls:
            env["WINEDLLOVERRIDES"] = ";".join(f"{d}=n,b" for d in dlls)
            notes.append("game DLLs")
    if "godot_vulkan" in on:
        args += ["--rendering-driver", "vulkan"]
        notes.append("Vulkan")
    elif "godot_opengl" in on:
        args += ["--rendering-driver", "opengl3"]
        notes.append("OpenGL")
    if "pulse_latency" in on:
        env["PULSE_LATENCY_MSEC"] = "60"
        notes.append("audio buffer")
    if cfg.fps_limit > 0:                          # DirectX through DXVK (9–11) and VKD3D-Proton (12)
        env["DXVK_FRAME_RATE"] = env["VKD3D_FRAME_RATE"] = str(cfg.fps_limit)
        notes.append(f"{cfg.fps_limit} FPS limit")
    if "pcores" in on:                             # the whole game (Proton, wineserver, its threads) on the P-cores
        from ezgd.platform.machine import performance_cpus
        cpus, taskset = performance_cpus(), shutil.which("taskset")
        if cpus and taskset:
            wrappers += [taskset, "-c", cpus]
            notes.append("performance cores")
    for key, tool in (("gamemode", "gamemoderun"), ("mangohud", "mangohud")):
        if key in on:
            path = shutil.which(tool)
            if path:
                wrappers.append(path)
                notes.append(tool)
            else:
                notes.append(f"{tool} missing — skipped")
    return FixEffects(env, wrappers, args, notes)
