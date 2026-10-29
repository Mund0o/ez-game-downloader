"""Fix generator: the debugging assistant builds its own fixes from the evidence (and the symptom the user picks).

Generated fixes are concrete, reversible changes — environment variables and arguments in the launch options
(Steam syntax, visible in Properties), WINEDLLOVERRIDES entries, Proton options, or an edit of the game's own
config file (always with a backup). They are not applied blindly: "Try fixes for me" tests them in a throw-away
prefix and only the ones that kept the game running are offered. Fixes that worked are remembered (engine +
problem → fix) and tried first for similar games.
"""

from __future__ import annotations

import json
import re
import shutil
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from ezgd.platform.launcher import COMMAND_TOKEN

SYMPTOMS = {
    "no_start": "Doesn't start / closes right away",
    "crash_later": "Crashes after a while",
    "black_screen": "Black screen or no picture",
    "no_sound": "No sound",
    "slow": "Runs badly / stutters",
    "controller": "Controller / gamepad doesn't work",
    "online": "Can't play online / with friends",
}

# DLLs that are usually add-ons injected into the game (overlays, ReShade, mod loaders) when found in its folder
_INJECTED = {
    "dxgi.dll": "ReShade / an overlay (dxgi.dll)", "d3d11.dll": "a graphics add-on (d3d11.dll)",
    "d3d9.dll": "ReShade / an add-on (d3d9.dll)", "opengl32.dll": "ReShade / an add-on (opengl32.dll)",
    "dinput8.dll": "a mod loader (dinput8.dll)", "version.dll": "a mod loader (version.dll)",
    "winhttp.dll": "BepInEx / a mod loader (winhttp.dll)", "d3d12.dll": "a graphics add-on (d3d12.dll)",
}


@dataclass
class GeneratedFix:
    gen_id: str                      # stable id of the idea (used for learning), e.g. "gpu:discrete"
    label: str
    why: str                         # the evidence it is built from
    env: dict[str, str] = field(default_factory=dict)
    args: str = ""                   # game arguments
    dll_overrides: dict[str, str] = field(default_factory=dict)   # dll → mode ("b", "n,b", "")
    options: list[str] = field(default_factory=list)              # Proton option keys
    edits: list[dict] = field(default_factory=list)               # {"path", "key", "value", "kind": "keyval"}
    learned_from: str = ""
    raw_options: str = ""            # a complete launch-option line (from ProtonDB) used as-is
    fixes_on: list[str] = field(default_factory=list)    # Fixes-screen switches to turn on
    fixes_off: list[str] = field(default_factory=list)   # … and off
    compat_set: dict = field(default_factory=dict)       # CompatConfig fields (use_runtime, protonfixes)
    winetricks: list[str] = field(default_factory=list)  # installed into the test copy first (into the real prefix
                                                         # only once it has worked there)
    proton_kind: str = ""            # "ge" / "cachyos" / "valve": a Proton of that kind (shareable — no names)
    proton_version: str = ""         # … the one installed here that stands for it (resolved locally, never kept)

    def proton_vars(self) -> list[str]:
        """The PROTON_* variables this fix relies on (to skip it on a Proton build that does not read them)."""
        from ezgd.platform.proton import OPTIONS_BY_KEY
        out = [k for k in self.env if k.startswith("PROTON_")]
        out += [OPTIONS_BY_KEY[o].env for o in self.options if o in OPTIONS_BY_KEY
                and OPTIONS_BY_KEY[o].env.startswith("PROTON_")]
        return out

    def overrides(self, entry) -> dict:
        """Launch-plan overrides that express this fix on top of the game's current setup."""
        o: dict = {}
        if self.raw_options:
            return {"launch_args": self.raw_options}
        opts = entry.launch_args or ""
        if self.env:
            opts = with_env(opts, self.env)
        if self.args:
            from ezgd.library.advisor import with_game_args
            opts = with_game_args(opts, self.args)
        if opts != (entry.launch_args or ""):
            o["launch_args"] = opts
        if self.dll_overrides or self.options or self.fixes_on or self.fixes_off or self.compat_set:
            c = entry.compat_config.to_dict()
            if self.fixes_on or self.fixes_off:
                c["fixes"] = sorted((set(c.get("fixes") or []) | set(self.fixes_on)) - set(self.fixes_off))
            c.update(self.compat_set)
            if self.dll_overrides:
                parts = {p.split("=", 1)[0].lower(): p for p in (c.get("dll_overrides") or "").split(";") if p}
                for dll, mode in self.dll_overrides.items():
                    parts[dll.lower()] = f"{dll}={mode}"
                c["dll_overrides"] = ";".join(parts.values())
            if self.options:
                c["options"] = sorted(set(c.get("options") or []) | set(self.options))
            o["compat"] = c
        if self.edits:
            o["edits"] = self.edits
        if self.winetricks:
            o["winetricks"] = list(self.winetricks)
        if self.proton_version:
            o["proton_version"] = self.proton_version
        return o

    def to_dict(self) -> dict:
        return {"gen_id": self.gen_id, "label": self.label, "why": self.why, "env": self.env, "args": self.args,
                "raw_options": self.raw_options,
                "dll_overrides": self.dll_overrides, "options": self.options, "edits": self.edits,
                "learned_from": self.learned_from, "fixes_on": self.fixes_on, "fixes_off": self.fixes_off,
                "compat_set": self.compat_set, "winetricks": self.winetricks, "proton_kind": self.proton_kind}


def with_env(options: str, env: dict[str, str]) -> str:
    """Put ``VAR=value`` assignments in front of the game command (Steam launch-option syntax)."""
    assign = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    options = options.strip()
    if not options:
        return f"{assign} {COMMAND_TOKEN}"
    if COMMAND_TOKEN in options:
        return options.replace(COMMAND_TOKEN, f"{assign} {COMMAND_TOKEN}", 1)
    return f"{assign} {COMMAND_TOKEN} {options}"


# --------------------------------------------------------------------- generators
def _gpu(ctx, symptom: str, findings: list[dict]) -> list[GeneratedFix]:
    gpus = getattr(ctx.system, "gpus", None) or []
    if len(gpus) < 2:
        return []
    discrete = next((g for g in gpus if re.search(r"(nvidia|geforce|rtx|gtx|radeon rx|radeon pro|arc a)", g.name,
                                                    re.I)), None)
    if discrete is None:
        return []
    graphics = symptom in ("black_screen", "slow", "no_start") or any(
        k in f["key"] for f in findings for k in ("graphics", "vulkan", "dx12", "dump-graphics"))
    if not graphics:
        return []
    nvidia = "nvidia" in discrete.name.lower() or "geforce" in discrete.name.lower()
    key = re.sub(r"^(NVIDIA |AMD )", "", discrete.name)
    env = {"DXVK_FILTER_DEVICE_NAME": key, "VKD3D_FILTER_DEVICE_NAME": key}
    env.update({"__NV_PRIME_RENDER_OFFLOAD": "1", "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
                "__VK_LAYER_NV_optimus": "NVIDIA_only"} if nvidia else {"DRI_PRIME": "1"})
    others = ", ".join(g.name for g in gpus if g is not discrete)
    return [GeneratedFix("gpu:discrete", f"Force the {discrete.name}",
                         f"This PC has {len(gpus)} GPUs ({discrete.name} and {others}). A game drawing on the "
                         "integrated one gives a black screen or very low speed; this makes DXVK, vkd3d and OpenGL use "
                         f"the {discrete.name}.", env=env)]


def _injected(ctx, findings: list[dict]) -> list[GeneratedFix]:
    out = []
    crash_mods = set()
    for f in findings:
        for ev in f.get("evidence", []):
            m = re.search(r"inside ([\w.\-]+\.dll)|= ([\w.\-]+\.dll)\+0x", ev, re.I)
            if m:
                crash_mods.add((m.group(1) or m.group(2)).lower())
    for dll, what in _INJECTED.items():
        p = ctx.exe.parent / dll
        if not p.exists():
            continue
        crashed = dll in crash_mods
        out.append(GeneratedFix(f"disable:{dll}", f"Ignore the game folder's {dll}",
                                f"The game folder contains {dll}, usually {what}"
                                + (", and the crash happened inside it" if crashed else "") +
                                f". This makes Wine use its own {dll} instead (the file is not deleted).",
                                dll_overrides={dll[:-4]: "b"}))
    return out


def _nvapi(ctx, findings: list[dict]) -> list[GeneratedFix]:
    text = ctx.everything.lower()
    if not ("nvapi" in text and re.search(r"(err:|crash|unhandled|failed)", text)) and \
            not any("nvapi" in " ".join(f.get("evidence", [])).lower() for f in findings):
        return []
    return [GeneratedFix("nvapi:off", "Turn off NVAPI", "The logs show errors from NVAPI (NVIDIA's DLSS/Reflex "
                         "interface). Turning it off avoids that code path.",
                         env={"PROTON_DISABLE_NVAPI": "1", "DXVK_ENABLE_NVAPI": "0"})]


_RENDERERS = {
    "Unity": [("-force-vulkan", "Unity's Vulkan renderer"), ("-force-d3d11", "Unity's DirectX 11 renderer"),
              ("-force-glcore", "Unity's OpenGL renderer"),
              ("-force-d3d11-no-singlethreaded", "Unity's DirectX 11 renderer without its render thread")],
    "Unreal Engine": [("-dx11", "Unreal's DirectX 11 renderer"), ("-vulkan", "Unreal's Vulkan renderer"),
                      ("-dx12", "Unreal's DirectX 12 renderer")],
    "Godot": [("--rendering-driver vulkan", "Godot's Vulkan renderer"),
              ("--rendering-driver opengl3", "Godot's OpenGL renderer")],
}


def _renderers(ctx, symptom: str, findings: list[dict]) -> list[GeneratedFix]:
    if ctx.engine not in _RENDERERS:
        return []
    graphics = symptom in ("black_screen", "no_start", "crash_later") or any(
        k in f["key"] for f in findings for k in ("graphics", "vulkan", "dx12", "crash", "dump"))
    if not graphics:
        return []
    # what is already in effect: launch options and the Fixes screen's renderer switches
    have = (ctx.entry.launch_args or "") + " " + " ".join(
        {"godot_vulkan": "--rendering-driver vulkan", "godot_opengl": "--rendering-driver opengl3"}.get(k, "")
        for k in ctx.entry.compat_config.fixes)
    out = []
    for arg, what in _RENDERERS[ctx.engine]:
        if arg in have:
            continue
        out.append(GeneratedFix(f"renderer:{ctx.engine}:{arg}", f"Start with {what}",
                                f"{ctx.exe.name} is a {ctx.engine} game; {ctx.engine} accepts “{arg}” to switch "
                                "renderer, which avoids a renderer that fails under Proton.", args=arg))
    return out


def _windowed(ctx, symptom: str) -> list[GeneratedFix]:
    if symptom != "black_screen":
        return []
    arg = {"Unity": "-screen-fullscreen 0 -popupwindow", "Unreal Engine": "-windowed",
           "Godot": "--windowed"}.get(ctx.engine, "-windowed")
    return [GeneratedFix(f"windowed:{ctx.engine or 'generic'}", "Start in a window",
                         "A black screen at start is often exclusive fullscreen failing on the desktop's compositor; a "
                         f"window avoids it (adds “{arg}”; switch back to fullscreen in the game's settings once it "
                         "shows).",
                         args=arg)]


def _unity_graphics_jobs(ctx, symptom: str, findings: list[dict]) -> list[GeneratedFix]:
    if ctx.engine != "Unity":
        return []
    boot = next(iter(ctx.exe.parent.glob("*_Data/boot.config")), None)
    if boot is None:
        return []
    try:
        text = boot.read_text("utf-8", "replace")
    except OSError:
        return []
    if not re.search(r"^gfx-enable-native-gfx-jobs=1\s*$", text, re.M):
        return []
    crashy = symptom in ("crash_later", "no_start", "black_screen") or any(
        k in f["key"] for f in findings for k in ("crash", "dump"))
    if not crashy:
        return []
    return [GeneratedFix("unity:gfx-jobs-off", "Turn off Unity's native graphics jobs",
                         f"{boot.relative_to(ctx.exe.parent)} enables native graphics jobs, a Unity feature that "
                         "crashes or hangs some games under DXVK. This edits that one line (a backup is kept).",
                         edits=[{"path": str(boot), "kind": "keyval", "key": "gfx-enable-native-gfx-jobs",
                                 "value": "0"}])]


# ----------------------------------------------------------- Unreal Engine: the game's own settings files
def _ue_config_dirs(ctx) -> list[Path]:
    """Where an Unreal game keeps its settings: <prefix>/…/AppData/Local/<Project>/Saved/Config/Windows (UE5) or
    …/WindowsNoEditor (UE4). Both when the game has not written either yet."""
    if ctx.engine != "Unreal Engine" or getattr(ctx, "prefix_dir", None) is None:
        return []
    exe = ctx.exe
    ships = [exe] if exe.name.lower().endswith("-shipping.exe") else \
        sorted(exe.parent.glob("*/Binaries/Win64/*-Shipping.exe"))
    if not ships or len(ships[0].parents) < 3:
        return []
    project = ships[0].parents[2].name
    base = ctx.prefix_dir / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / project / "Saved" / \
        "Config"
    have = [base / d for d in ("Windows", "WindowsNoEditor") if (base / d).is_dir()]
    return have or [base / "Windows", base / "WindowsNoEditor"]


def _ue_edits(dirs: list[Path], name: str, section: str, values: dict[str, str]) -> list[dict]:
    return [{"path": str(d / name), "kind": "ini", "section": section, "key": k, "value": v, "create": True}
            for d in dirs for k, v in values.items()]


def _unreal(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """Edits to an Unreal game's own settings (backed up; tested on a copy first): DirectX 11 in its config for
    games that ignore -dx11, windowed mode for black screens, lower settings / no ray tracing for GPU crashes and
    out-of-memory, and no start-up movies (they need Windows' media components)."""
    dirs = _ue_config_dirs(ctx)
    if not dirs:
        return []
    keys = " ".join(f["key"] for f in findings)
    graphics = deep or symptom in ("no_start", "crash_later", "black_screen") or any(
        k in keys for k in ("dx12", "gpu", "vulkan", "graphics", "shaders", "vram", "crash", "dump"))
    out: list[GeneratedFix] = []
    rel = "AppData/Local/…/Saved/Config"
    if graphics:
        out.append(GeneratedFix(
            "ue:config-dx11", "Set DirectX 11 in the game's settings file",
            "Some Unreal games ignore “-dx11” on the command line; their Engine.ini decides the renderer. This sets "
            f"DefaultGraphicsRHI to DirectX 11 there ({rel}, a backup is kept).",
            edits=_ue_edits(dirs, "Engine.ini", "/Script/WindowsTargetPlatform.WindowsTargetSettings",
                            {"DefaultGraphicsRHI": "DefaultGraphicsRHI_DX11"})))
    if symptom == "black_screen" or deep:
        out.append(GeneratedFix(
            "ue:config-windowed", "Switch the game to windowed mode in its settings",
            "A black screen in an Unreal game is often its saved fullscreen mode failing; GameUserSettings.ini is set "
            "to windowed (FullscreenMode=2) — switch back in the game's options once it shows.",
            edits=_ue_edits(dirs, "GameUserSettings.ini", "/Script/Engine.GameUserSettings",
                            {"FullscreenMode": "2", "LastConfirmedFullscreenMode": "2",
                             "PreferredFullscreenMode": "2"})))
    if symptom in ("crash_later", "slow") or deep or any(k in keys for k in ("vram", "gpu", "memory", "dump")):
        low = {f"sg.{g}Quality": "1" for g in ("ViewDistance", "AntiAliasing", "Shadow", "GlobalIllumination",
                                               "Reflection", "PostProcess", "Texture", "Effects", "Foliage",
                                               "Shading")}
        out.append(GeneratedFix(
            "ue:config-low", "Lower the game's graphics settings",
            "Crashes after a while and graphics-device errors in Unreal games are often the GPU running out of "
            "memory; this sets the game's quality levels to Low in GameUserSettings.ini (raise them again in the "
            "game once it is stable).",
            edits=_ue_edits(dirs, "GameUserSettings.ini", "ScalabilityGroups", low)))
        out.append(GeneratedFix(
            "ue:config-no-rt", "Turn off ray tracing in the game's settings",
            "Ray tracing (and Lumen's hardware ray tracing) is the least stable part of Unreal games under Proton; "
            "Engine.ini turns it off.",
            edits=_ue_edits(dirs, "Engine.ini", "SystemSettings",
                            {"r.RayTracing": "0", "r.Lumen.HardwareRayTracing": "0"})))
    if symptom in ("no_start", "black_screen") or deep or any(k in keys for k in ("media", "mf", "video")):
        out.append(GeneratedFix(
            "ue:nomovie", "Skip the start-up movies",
            "Unreal games play their intro videos with Windows' media components, which Proton does not always "
            "have; “-nomovie” skips them (a common cause of crashes, hangs or a black screen right at start).", args="-nomovie"))
    return out


def _audio(ctx, symptom: str) -> list[GeneratedFix]:
    if symptom != "no_sound":
        return []
    out = [GeneratedFix("audio:latency", "Larger audio buffer",
                        "Crackling or missing sound under PipeWire is often a too-small buffer; this sets "
                        "PULSE_LATENCY_MSEC=90 for the game.", env={"PULSE_LATENCY_MSEC": "90"})]
    shipped = [p.name.lower() for p in ctx.exe.parent.glob("xaudio2_*.dll")]
    if shipped:
        out.append(GeneratedFix("audio:native-xaudio", "Use the game's own XAudio2",
                                f"The game ships {', '.join(shipped)}; loading it instead of Wine's version fixes "
                                "sound in some games.", dll_overrides={n[:-4]: "n,b" for n in shipped}))
    from ezgd.library import probes
    seen = probes.from_text(ctx.everything) or {}
    wine = " ".join((seen.get("facts") or {}).get("wine_errors") or [])
    if seen.get("cause") == "no-stream" and re.search(r"xaudio|xact|x3daudio", wine + ctx.everything[-200_000:], re.I):
        verb = "xact" if getattr(ctx, "is32", None) else "xact_x64"
        out.append(GeneratedFix("audio:xact", "Install DirectX audio (XACT / XAudio2)",
                                "The game opened no audio stream and Wine's log names DirectX audio; Microsoft's own "
                                "XACT / XAudio2 replaces Wine's.", winetricks=[verb]))
    return out


# what each measured cause of "runs badly" points at (ezgd.library.probes.causes), by fix id prefix
_PERF_FIXES = {"shaders": ("cache:", "vkd3d:", "sync:"), "stutter": ("sync:", "cpu:", "cache:"),
               "memory": ("ue:config-low", "mem:", "heap:"), "gpu": ("ue:config-low", "gpu:", "vkd3d:"),
               "cpu": ("sync:", "cpu:", "toggle:"), "cpu-busy": ("cache:", "sync:", "cpu:")}


def _by_evidence(ctx, symptom: str, fixes: list[GeneratedFix]) -> list[GeneratedFix]:
    """What the test run saw of the reported problem (ezgd.library.probes) orders the fixes — and drops those it rules
    out: no controller connected, or sound going to a muted / other output, is nothing a game setting changes."""
    from ezgd.library import probes
    text = getattr(ctx, "everything", "") or ""
    seen = probes.from_text(text) or {}
    cause = seen.get("cause", "")
    first: tuple[str, ...] = ()
    if symptom == "controller":
        if cause == "none-connected":
            return [f for f in fixes if not f.gen_id.startswith("input:")]
        first = {"doubled": ("input:no-hidraw", "input:no-steaminput"), "doubled-steam": ("input:no-steaminput",),
                 "none-seen": ("input:sdl", "input:no-hidraw")}.get(cause, ())
    elif symptom == "no_sound":
        if cause in ("muted-output", "muted-stream", "other-output"):
            return [f for f in fixes if not f.gen_id.startswith("audio:")]
        first = ("audio:xact", "audio:native-xaudio", "audio:latency") if cause in ("no-stream", "silent") else ()
    elif symptom == "slow":
        perf = probes.perf_from_text(text)
        for key, _words in probes.causes(perf) if perf else []:
            first += _PERF_FIXES.get(key, ())
    if not first:
        return fixes

    def rank(f: GeneratedFix) -> int:
        return next((i for i, pre in enumerate(first) if f.gen_id.startswith(pre)), len(first))
    return sorted(fixes, key=rank)


def _memory(ctx, symptom: str, findings: list[dict]) -> list[GeneratedFix]:
    if not ctx.is32 or "laa" in ctx.entry.compat_config.options:
        return []
    oom = re.search(r"(out of memory|E_OUTOFMEMORY|bad_alloc|Could not allocate|virtual memory)", ctx.everything,
                    re.I)
    if not (oom or symptom == "crash_later"):
        return []
    return [GeneratedFix("mem:laa", "Let the 32-bit game use 4 GB of memory",
                         ("The logs show memory allocation failures. " if oom else "") +
                         "32-bit games are limited to 2 GB unless they are large-address-aware; Proton can force it.",
                         options=["laa"])]


def _community(ctx) -> list[GeneratedFix]:
    """Launch options ProtonDB players used when they got this game working (sanitized; see protondb.py)."""
    from ezgd.library import protondb
    c = getattr(ctx, "community", None) or {}
    out = []
    ok = c.get("ok", 0)
    have = ctx.entry.launch_args or ""
    for opts, n in c.get("opts", []):
        if not protondb.is_fix(opts) or opts in have:
            continue
        short = opts.replace(" %command%", " … ").replace("%command%", "…")
        import hashlib
        gid = "protondb:" + hashlib.sha1(opts.encode()).hexdigest()[:10]
        f = GeneratedFix(gid, f"Players' fix: {short.strip()[:70]}",
                         f"{n} of the {ok} ProtonDB players who got {c.get('t') or 'this game'} working used these "
                         "launch options (ProtonDB, ODbL).")
        f.raw_options = opts                     # replaces the options (they already contain %command%)
        out.append(f)
        if len(out) >= 3:
            break
    return out


def _troubled(ctx, symptom: str, findings: list[dict], deep: bool) -> bool:
    """Does the game fail to start / crash (or did the user ask to try everything)?"""
    if deep or symptom in ("no_start", "crash_later", "black_screen"):
        return True
    run = getattr(ctx, "run", None)
    quick = run is not None and run.found and run.exit_code not in (0, None) and (run.seconds or 0) < 120
    return quick or any(k in f["key"] for f in findings for k in ("crash", "dump", "exit", "wrong-exe"))


def _sync(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """Wine's fast synchronisation (fsync/esync) hangs or crashes a few games."""
    if not _troubled(ctx, symptom, findings, deep) or not ctx.exe.suffix.lower() == ".exe":
        return []
    have = set(ctx.entry.compat_config.options)
    out = []
    if "no_fsync" not in have:
        out.append(GeneratedFix("sync:no-fsync", "Turn off fsync",
                                "Some games hang at start or crash with Wine's fsync; this is a common, harmless test.",
                                options=["no_fsync"]))
    if not {"no_fsync", "no_esync"} <= have:
        out.append(GeneratedFix("sync:none", "Turn off fsync and esync",
                                "The slowest but most compatible synchronisation; fixes games that freeze or crash "
                                "with both fast modes.", options=["no_fsync", "no_esync"]))
    return out


_CPU_EVIDENCE = re.compile(r"(too many (?:cores|processors|cpus|threads)|processor count|NUMBER_OF_PROCESSORS|"
                           r"GetLogicalProcessorInformation\w* failed|thread (?:limit|count) (?:exceeded|too)|"
                           r"(?:CreateThread|pthread_create) failed|Failed to create (?:worker |job )?thread|"
                           r"resource temporarily unavailable.*thread|Unsupported (?:CPU|processor) (?:count|topology)|"
                           r"affinity mask)", re.I)


def _cpu_cores(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """Older games crash on CPUs with many cores (thread-count overflows). Offered when the run's logs talk about
    processor or thread counts — or, on a big CPU, as one guess among the generic fixes."""
    import os
    n = os.cpu_count() or 0
    m = _CPU_EVIDENCE.search(getattr(ctx, "everything", "") or "") if n > 8 else None
    if m is None and (n < 16 or not _troubled(ctx, symptom, findings, deep)):
        return []
    why = (f"The game's log says “{m.group(0)}” and this computer has {n} CPU threads — games written for fewer "
           "trip over that many. " if m else
           f"This computer has {n} CPU threads; some (mostly older) games crash or hang when they see that many. ")
    return [GeneratedFix("cpu:8", "Show the game only 8 CPU cores", why + "WINE_CPU_TOPOLOGY makes the game see 8.",
                         env={"WINE_CPU_TOPOLOGY": "8:" + ",".join(str(i) for i in range(8))})]


def _locale(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    if not _troubled(ctx, symptom, findings, deep):
        return []
    import os
    lang = os.environ.get("LANG", "")
    if lang.startswith("en_US"):
        return []
    return [GeneratedFix("locale:en", "Start the game with an English (US) locale",
                         f"Some games crash on number/date formats of other languages (system locale: {lang or 'unset'}).",
                         env={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})]


def _setup_toggles(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """Undo parts of the current setup one at a time — the culprit is sometimes a switch that is on."""
    if not _troubled(ctx, symptom, findings, deep):
        return []
    cfg = ctx.entry.compat_config
    out = []
    desc = getattr(getattr(ctx, "run", None), "description", "") or ""
    if ("Steam overlay" in desc or "overlay loaded" in desc) and \
            "no_steam_overlay" not in cfg.fixes:
        out.append(GeneratedFix("toggle:no-overlay", "Without the Steam overlay",
                                "The last start had the Steam overlay loaded; a few games crash with it.",
                                fixes_on=["no_steam_overlay"]))
    for key, what in (("gamemode", "GameMode"), ("mangohud", "MangoHud")):
        if key in cfg.fixes:
            out.append(GeneratedFix(f"toggle:no-{key}", f"Without {what}",
                                    f"{what} is switched on for this game; it can crash games that load it.",
                                    fixes_off=[key]))
    if "game_dlls" in cfg.fixes:
        out.append(GeneratedFix("toggle:no-game-dlls", "Without the game's own DLLs",
                                "The game's own DLL copies are loaded first; if one of them is broken or the wrong "
                                "version, Wine's builtin one works better.", fixes_off=["game_dlls"]))
    if cfg.use_runtime and ctx.exe.suffix.lower() == ".exe":
        out.append(GeneratedFix("toggle:no-runtime", "Outside the Steam Linux Runtime container",
                                "The container occasionally cannot see a drive or a library the game needs.",
                                compat_set={"use_runtime": False}))
    if cfg.protonfixes and ctx.exe.suffix.lower() == ".exe":
        out.append(GeneratedFix("toggle:no-protonfixes", "Without protonfixes",
                                "A per-game protonfix written for the Steam version can break a different release.",
                                compat_set={"protonfixes": False}))
    return out


def _vkd3d(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    dx12 = any("dx12" in f["key"] for f in findings) or "d3d12" in (ctx.everything or "").lower()[:200000]
    if not dx12 or ctx.exe.suffix.lower() != ".exe":
        return []
    return [GeneratedFix("vkd3d:fl12_0", "Report DirectX 12 feature level 12_0",
                         "The game's DirectX 12 renderer may reject what VKD3D reports; feature level 12_0 is the "
                         "most widely accepted.", env={"VKD3D_FEATURE_LEVEL": "12_0"})]


def _ntsync(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """ntsync (the kernel's new Wine synchronisation, used by GE and CachyOS builds) is new; a few games hang or
    crash with it. PROTON_NO_NTSYNC=1 is read by both builds."""
    if not _troubled(ctx, symptom, findings, deep):
        return []
    if not re.search(r"ntsync: up and running", ctx.everything or "") and not deep:
        return []
    return [GeneratedFix("sync:no-ntsync", "Turn off ntsync",
                         "The game ran with ntsync (the kernel's new Wine synchronisation). It is new, and a few "
                         "games hang or crash with it; this falls back to fsync.", env={"PROTON_NO_NTSYNC": "1"})]


def _vkd3d_more(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    text = (ctx.everything or "")[:400000].lower()
    dx12 = any("dx12" in f["key"] for f in findings) or "vkd3d" in text or "d3d12" in text
    if not dx12 or not _troubled(ctx, symptom, findings, deep):
        return []
    out = [GeneratedFix("vkd3d:nodxr", "DirectX 12 without ray tracing",
                        "Ray tracing (DXR) through VKD3D crashes some games at start or when loading; the game then "
                        "uses its normal lighting.", env={"VKD3D_CONFIG": "nodxr"})]
    gpus = " ".join(getattr(g, "name", "") for g in (getattr(ctx.system, "gpus", None) or []))
    if "nvidia" in gpus.lower() or "geforce" in gpus.lower() or "rtx" in gpus.lower():
        out.append(GeneratedFix("vkd3d:no-upload-hvv", "DirectX 12 without the NVIDIA upload-heap path",
                                "On NVIDIA cards VKD3D's use of “resizable BAR” memory makes some DirectX 12 games "
                                "crash or stutter; this turns it off.", env={"VKD3D_CONFIG": "no_upload_hvv"}))
    return out


def _shader_cache(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """A damaged shader cache crashes a game that used to work: moved aside, the game rebuilds it."""
    if not _troubled(ctx, symptom, findings, deep) or ctx.exe.suffix.lower() != ".exe":
        return []
    root = Path(ctx.entry.install_path or ctx.exe.parent)
    found: list[Path] = []
    for d in {ctx.exe.parent, root}:
        for pat in ("*.dxvk-cache", "vkd3d-proton.cache", "vkd3d-proton.cache.write", "*.dxvk-cache.tmp"):
            found += [p for p in d.glob(pat) if p.is_file()]
    d3ds = ctx.prefix_dir / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "D3DSCache"
    if d3ds.is_dir():
        found.append(d3ds)
    if not found:
        return []
    return [GeneratedFix("cache:reset", "Rebuild the shader caches",
                         "Shader caches (" + ", ".join(sorted({p.name for p in found})[:3]) + ") are kept between "
                         "starts; a damaged one makes a game crash at start or while loading. The test moves them "
                         "aside (the game builds new ones — the first minutes can stutter).",
                         edits=[{"path": str(p), "kind": "move_aside"} for p in sorted(set(found))])]


def _pe(ctx):
    from ezgd.platform.pe import read_pe
    try:
        return read_pe(ctx.exe)
    except Exception:
        return None


def _heap(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """Use-after-free in the game: Windows' heap happens to tolerate it, Wine's does not."""
    text = (ctx.everything or "")[-2_000_000:]
    heapy = re.search(r"(err:heap|HEAP_|RtlFreeHeap|c0000374|heap corruption)", text, re.I) or \
        any(k in f["key"] for f in findings for k in ("exit:heap",))
    if not (heapy or (deep and _troubled(ctx, symptom, findings, deep))):
        return []
    return [GeneratedFix("heap:delay-free", "Delay freeing memory (Proton's heap workaround)",
                         "The log shows heap errors" if heapy else "Some games use memory after freeing it; Windows "
                         "tolerates that, Wine's heap does not. PROTON_HEAP_DELAY_FREE keeps freed memory a "
                         "little longer.", options=["heap_delay_free"])]


def _d3d8(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    info = _pe(ctx)
    if info is None or "d3d8.dll" not in info.imports + info.delay_imports:
        return []
    if "dxvk_d3d8" in ctx.entry.compat_config.options:
        return []
    return [GeneratedFix("d3d8:dxvk", "DirectX 8 through DXVK",
                         f"{ctx.exe.name} uses DirectX 8 (d3d8.dll). Proton runs that through WineD3D unless "
                         "PROTON_DXVK_D3D8 is set; DXVK draws many DirectX 8 games correctly and faster.",
                         options=["dxvk_d3d8"])]


def _wow64(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    info = _pe(ctx)
    if info is None or not info.is32 or not _troubled(ctx, symptom, findings, deep):
        return []
    return [GeneratedFix("wow64:on", "Run the 32-bit game in Wine's new WoW64 mode",
                         "This is a 32-bit game; Wine's WoW64 mode runs it differently (no 32-bit Linux libraries "
                         "involved), which avoids some 32-bit crashes.", options=["wow64"])]


def _game_drive(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    if not _troubled(ctx, symptom, findings, deep):
        return []
    path = str(ctx.entry.install_path or ctx.exe.parent)
    odd = len(path) > 120 or any(ord(ch) > 127 for ch in path)
    if not (odd or deep):
        return []
    return [GeneratedFix("drive:game", "Give the game's folder its own drive letter",
                         ("The game's folder path is long or has special characters; " if odd else "")
                         + "some games only work from a short drive path (PROTON_SET_GAME_DRIVE maps it to S:).",
                         options=["game_drive"])]


def _controller(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    if symptom != "controller":
        return []
    from ezgd.platform import steam
    out = [GeneratedFix("input:sdl", "Controllers through SDL", "Proton's SDL path recognises most controllers and "
                        "avoids doubled devices.", options=["prefer_sdl"]),
           GeneratedFix("input:no-steaminput", "Without Steam Input", "Steam Input translates controllers; some "
                        "games then see a keyboard or a wrong pad.", options=["no_steaminput"]),
           GeneratedFix("input:no-hidraw", "Without raw HID access", "Raw HID access shows some controllers twice "
                        "or with wrong buttons.", options=["no_hidraw"])]
    if steam.valve_controllers():
        # a Steam Controller / Steam Deck reaches a game only as Steam's virtual pad, which GE-Proton and
        # Proton-CachyOS ignore with these two (PROTON_NO_STEAMINPUT): the controller would do nothing at all
        out = [f for f in out if not {"prefer_sdl", "no_steaminput"} & set(f.options)]
    return out


def _no_d3d11(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    info = _pe(ctx)
    if info is None or not deep:
        return []
    imps = set(info.imports + info.delay_imports)
    if "d3d11.dll" not in imps or not ({"d3d10.dll", "d3d10_1.dll", "d3d9.dll"} & imps):
        return []
    return [GeneratedFix("d3d:no-d3d11", "Use the game's DirectX 10/9 mode",
                         "The game can also draw with an older DirectX; turning DirectX 11 off makes it use that.",
                         options=["no_d3d11"])]


def _windows_version(ctx, symptom, findings, deep) -> list[GeneratedFix]:
    """Old games built for Windows XP: some only start when the prefix says it is an older Windows."""
    info = _pe(ctx)
    if info is None or not _troubled(ctx, symptom, findings, deep):
        return []
    major = info.subsystem_version[0]
    if not major or major >= 6:
        return []
    return [GeneratedFix("winver:winxp", "Report Windows XP to the game",
                         f"{ctx.exe.name} was built for Windows XP (subsystem {major}.{info.subsystem_version[1]}). "
                         "Some old games refuse to start or misbehave on newer Windows versions; the test copy of "
                         "the prefix is set to Windows XP first.", winetricks=["winxp"]),
            GeneratedFix("winver:win7", "Report Windows 7 to the game",
                         "Between XP and 10: many games of that era work best with it.", winetricks=["win7"])]


GENERATORS = ("gpu", "injected", "nvapi", "renderers", "windowed", "unity", "audio", "memory", "community",
              "sync", "cpu", "locale", "toggles", "vkd3d", "ntsync", "vkd3d_more", "shader_cache", "heap", "d3d8",
              "wow64", "game_drive", "controller", "no_d3d11", "windows_version")


def generate(ctx, findings: list[dict], symptom: str = "", learned: list[dict] | None = None,
             deep: bool = False) -> list[GeneratedFix]:
    """``deep``: "Try everything" — every generic fix, not only the ones the evidence points at."""
    fixes: list[GeneratedFix] = []
    fixes += _gpu(ctx, symptom, findings)
    fixes += _injected(ctx, findings)
    fixes += _nvapi(ctx, findings)
    fixes += _renderers(ctx, symptom, findings)
    fixes += _windowed(ctx, symptom)
    if any(f["key"] == "display-mode" for f in findings):      # it could not switch the screen mode: windowed
        fixes += _windowed(ctx, "black_screen")
    fixes += _unity_graphics_jobs(ctx, symptom, findings)
    fixes += _audio(ctx, symptom)
    fixes += _memory(ctx, symptom if not deep else "crash_later", findings)
    fixes += _community(ctx)
    for gen in (_sync, _cpu_cores, _locale, _setup_toggles, _vkd3d, _ntsync, _vkd3d_more, _shader_cache, _heap,
                _d3d8, _wow64, _game_drive, _controller, _no_d3d11, _windows_version, _unreal):
        try:
            fixes += gen(ctx, symptom, findings, deep)
        except Exception:                          # one generator must not stop the others
            continue
    if deep:                                       # renderers / windowed even without a matching symptom
        fixes += _renderers(ctx, "no_start", findings)
        fixes += _windowed(ctx, "black_screen")
    # fixes that worked before for the same engine and problem go first
    keys = {f["key"] for f in findings} | ({f"symptom:{symptom}"} if symptom else set())
    wins: dict[str, str] = {}
    for rec in learned or []:
        if rec.get("engine") == ctx.engine and rec.get("problem") in keys:
            wins.setdefault(rec["gen_id"], rec.get("title", ""))
    for f in fixes:
        if f.gen_id in wins:
            f.learned_from = wins[f.gen_id]
    fixes.sort(key=lambda f: not f.learned_from)
    # grounded: a fix built on a Proton switch the selected build does not read would test nothing
    from ezgd.platform.proton import proton_supports
    pdir = getattr(ctx, "proton_dir", None)
    fixes = [f for f in fixes if all(proton_supports(pdir, v) is not False for v in f.proton_vars())]
    seen, out = set(), []
    for f in fixes:
        if f.gen_id not in seen:
            seen.add(f.gen_id)
            out.append(f)
    try:
        out = _by_evidence(ctx, symptom, out)
    except Exception:                          # the order is a hint; never lose the fixes over it
        pass
    return out


# ------------------------------------------------------------------- file edits
def apply_edits(edits: list[dict], path_map: tuple[Path, Path] | None = None) -> list[tuple[Path, bytes | None]]:
    """Apply ``key=value`` edits; returns (file, original bytes) for :func:`revert_edits`. ``path_map`` redirects
    files inside the real prefix to a trial prefix."""
    done: list[tuple[Path, bytes | None]] = []
    for e in edits:
        p = Path(e["path"])
        if path_map and str(p).startswith(str(path_map[0]) + "/"):
            p = path_map[1] / p.relative_to(path_map[0])
        if e.get("kind") == "move_aside":           # e.g. a shader cache: the game rebuilds it
            if p.exists():
                dst = p.with_name(p.name + ".ezgd-trial")
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
                p.rename(dst)
                done.append((p, ("move", dst)))
            continue
        if e.get("kind") == "ini":                  # "[section]" + key=value (Unreal's config files)
            existed = p.is_file()
            if not existed and not e.get("create"):
                continue
            original = p.read_bytes() if existed else None
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(set_ini(original.decode("utf-8", "replace") if original else "", e["section"], e["key"],
                                 str(e["value"])), "utf-8")
            done.append((p, original if existed else ("created",)))
            continue
        if e.get("kind") != "keyval" or not p.is_file():
            continue
        original = p.read_bytes()
        text = original.decode("utf-8", "replace")
        line = f"{e['key']}={e['value']}"
        new, n = re.subn(rf"^{re.escape(e['key'])}=.*$", line, text, flags=re.M)
        if not n:
            new = text.rstrip("\n") + "\n" + line + "\n"
        p.write_text(new, "utf-8")
        done.append((p, original))
    return done


def set_ini(text: str, section: str, key: str, value: str) -> str:
    """``key=value`` in ``[section]`` of an INI text (the section is added when missing; other lines untouched)."""
    lines = text.splitlines()
    head = f"[{section}]"
    start = next((i for i, ln in enumerate(lines) if ln.strip().lower() == head.lower()), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [head, f"{key}={value}"]
        return "\n".join(lines) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        if lines[i].split("=", 1)[0].strip().lower() == key.lower():
            lines[i] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, f"{key}={value}")
    return "\n".join(lines) + "\n"


def revert_edits(done: list[tuple[Path, object]]) -> None:
    for p, original in reversed(done):
        if isinstance(original, tuple) and original[0] == "created":
            try:
                p.unlink()
            except OSError:
                pass
        elif isinstance(original, tuple) and original[0] == "move":
            if p.exists():                              # what the game rebuilt during the test
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
            original[1].rename(p)
        elif original is not None:
            p.write_bytes(original)


def backup_and_apply(edits: list[dict]) -> list[str]:
    """Permanent version (when the user keeps a setup): writes ``<file>.ezgd-backup-<time>`` first."""
    backups = []
    backups_of: set[str] = set()
    for e in edits:
        p = Path(e["path"])
        if e.get("kind") == "move_aside":
            if p.exists():
                b = p.with_name(f"{p.name}.ezgd-backup-{time.strftime('%Y%m%d-%H%M%S')}")
                p.rename(b)
                backups.append(str(b))
            continue
        if p.is_file() and str(p) not in backups_of:
            b = p.with_name(f"{p.name}.ezgd-backup-{time.strftime('%Y%m%d-%H%M%S')}")
            b.write_bytes(p.read_bytes())
            backups.append(str(b))
            backups_of.add(str(p))
    apply_edits([e for e in edits if e.get("kind") != "move_aside"])
    return backups


# ------------------------------------------------------------------------ learning
def load_learned(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def remember(path: Path, *, engine: str | None, problems: list[str], gen_id: str, title: str) -> None:
    recs = load_learned(path)
    for prob in problems:
        recs.append({"engine": engine, "problem": prob, "gen_id": gen_id, "title": title, "at": time.time()})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(recs[-500:]), "utf-8")
    tmp.replace(path)
