"""Debugging assistant: find out why a game does not run, from evidence, and recommend the fix that matches it.

Evidence is collected from the game's last run (the app's launch log, including Wine/Proton output — detailed
when the run was a *diagnostic launch*), the engine's own logs (Unity ``Player.log``, Unreal ``Saved/Logs``,
Godot), crash markers next to the game, the game's files and the system (Vulkan drivers).

Every finding cites the lines that prove it. A fix is only recommended when the evidence points to one; when a
problem has no fix under Proton the assistant says so, and when nothing matches it says that too instead of
guessing.
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ezgd.library import advisor
from ezgd.library.executables import find_executables, rank
from ezgd.library.models import LibraryEntry
from ezgd.platform.launcher import is_windows_binary

READ_LIMIT = 6 * 1024 * 1024         # tail of the app's game log that is analysed
ENGINE_LOG_LIMIT = 400 * 1024


@dataclass
class Fix:
    kind: str            # enable_fix | set_executable | add_args | winetricks | proton | system | reset_prefix
    value: str
    label: str           # button text

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "label": self.label}


@dataclass
class Finding:
    key: str
    title: str
    explanation: str
    confidence: float                     # 0..1 — how strongly the evidence supports this cause
    evidence: list[str] = field(default_factory=list)
    fix: Fix | None = None
    fixable: bool = True                  # False: known cause without a fix under Proton (say so honestly)

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "explanation": self.explanation,
                "confidence": round(self.confidence, 2), "evidence": self.evidence[:8],
                "fix": self.fix.to_dict() if self.fix else None, "fixable": self.fixable}


@dataclass
class LastRun:
    started: str | None = None
    description: str = ""
    diagnostic: bool = False
    exit_code: int | None = None
    seconds: int | None = None
    text: str = ""                        # everything logged for that run

    @property
    def found(self) -> bool:
        return self.started is not None


@dataclass
class Diagnosis:
    summary: str
    findings: list[Finding]
    facts: list[str]
    run: dict
    suggest_diagnostic_run: bool
    generated: list[dict] = field(default_factory=list)     # fixes the assistant built (see ezgd.library.fixgen)
    symptom: str = ""
    generated_deep: list[dict] = field(default_factory=list)  # extra ones "Try everything" also tests
    reasoning: list[dict] = field(default_factory=list)       # the assistant's chain of thought, for the user

    def to_dict(self) -> dict:
        return {"summary": self.summary, "findings": [f.to_dict() for f in self.findings], "facts": self.facts,
                "run": self.run, "suggest_diagnostic_run": self.suggest_diagnostic_run,
                "generated": self.generated, "symptom": self.symptom, "generated_deep": self.generated_deep,
                "reasoning": self.reasoning}


# ------------------------------------------------------------------ evidence
_HEADER = re.compile(r"^=== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
_EXIT = re.compile(r"^=== exited with code (-?\d+) after (\d+)s$")


def last_run(log_path: Path) -> LastRun:
    """The most recent game launch in the app's log (winetricks/winecfg runs are skipped)."""
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as fh:
            if size > READ_LIMIT:
                fh.seek(size - READ_LIMIT)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return LastRun()
    lines = text.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        m = _HEADER.match(lines[i])
        if m and not m.group(2).startswith(("winetricks", "exited")):
            start = i
            break
    if start is None:
        return LastRun()
    run = LastRun(started=_HEADER.match(lines[start]).group(1), description=_HEADER.match(lines[start]).group(2))
    run.diagnostic = "diagnostic run" in run.description
    body = []
    for ln in lines[start + 1:]:
        m = _EXIT.match(ln)
        if m:
            run.exit_code, run.seconds = int(m.group(1)), int(m.group(2))
            break
        if _HEADER.match(ln):
            break
        body.append(ln)
    run.text = "\n".join(body)
    return run


def _tail(p: Path, limit: int = ENGINE_LOG_LIMIT) -> str:
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _newest(paths) -> Path | None:
    files = [Path(p) for p in paths if Path(p).is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def engine_of(exe: Path) -> str | None:
    d = exe.parent
    if (d / "UnityPlayer.dll").exists() or any(d.glob("*_Data/globalgamemanagers")):
        return "Unity"
    if re.search(r"-Win(64|32)-Shipping\.exe$", exe.name, re.I) or (d.parent.parent / "Engine").is_dir() or \
            any(d.glob("*/Binaries/Win64")) or (d / "Engine" / "Binaries").is_dir():
        return "Unreal Engine"
    if any(d.glob("*.pck")) or (d / "launch_vulkan.bat").exists():
        return "Godot"
    if (d / "renpy").is_dir():
        return "Ren'Py"
    if (d / "nw.dll").exists() or (d / "package.nw").exists() or (d / "www" / "js" / "rpg_core.js").exists():
        return "RPG Maker / NW.js"
    return None


def engine_logs(exe: Path, prefix_dir: Path, engine: str | None) -> tuple[str, str]:
    """(name, text) of the newest log the game's engine wrote (empty when there is none)."""
    users = prefix_dir / "pfx" / "drive_c" / "users" / "steamuser"
    cands: list[str] = []
    if engine in (None, "Unity"):
        cands += glob.glob(str(users / "AppData" / "LocalLow" / "*" / "*" / "Player.log"))
    if engine in (None, "Unreal Engine"):
        cands += glob.glob(str(exe.parent / "**" / "Saved" / "Logs" / "*.log"), recursive=True)[:50]
        cands += glob.glob(str(exe.parent.parent.parent / "*" / "Saved" / "Logs" / "*.log"))
        cands += glob.glob(str(users / "AppData" / "Local" / "*" / "Saved" / "Logs" / "*.log"))
    if engine in (None, "Godot"):
        cands += glob.glob(str(users / "AppData" / "Roaming" / "*" / "logs" / "godot*.log"))
    p = _newest(cands)
    return (str(p), _tail(p)) if p else ("", "")


def vulkan_icds() -> tuple[bool, bool]:
    """(64-bit Vulkan driver present, 32-bit Vulkan driver present) — from the installed ICD manifests."""
    files = glob.glob("/usr/share/vulkan/icd.d/*.json") + glob.glob("/etc/vulkan/icd.d/*.json")
    text = " ".join(_tail(Path(f), 4096) for f in files)
    has64 = bool(files)
    has32 = bool(re.search(r"(lib32|i386|i686|/usr/lib/[^\"]*32)", text)) or \
        any(re.search(r"(i686|i386|32)", Path(f).name) for f in files)
    return has64, has32


def pe_is_32bit(exe: Path) -> bool | None:
    try:
        with open(exe, "rb") as fh:
            head = fh.read(4096)
        off = int.from_bytes(head[0x3C:0x40], "little")
        if head[off:off + 4] != b"PE\0\0":
            return None
        return int.from_bytes(head[off + 4:off + 6], "little") == 0x14C
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- DLL → fix
_RUNTIME_DLLS: list[tuple[str, str, str]] = [
    # (dll regex, winetricks verb, what it is)
    (r"(vcruntime140(_1|_threads)?|msvcp140(_\d|_atomic_wait|_codecvt_ids)?|vcomp140|concrt140|vccorlib140|"
     r"mfc140u?|mfcm140u?|vcamp140)\.dll", "vcrun2022",
     "Microsoft Visual C++ 2015–2022 runtime"),
    (r"(msvcr120|msvcp120)\.dll", "vcrun2013", "Microsoft Visual C++ 2013 runtime"),
    (r"(msvcr110|msvcp110)\.dll", "vcrun2012", "Microsoft Visual C++ 2012 runtime"),
    (r"(msvcr100|msvcp100)\.dll", "vcrun2010", "Microsoft Visual C++ 2010 runtime"),
    (r"(msvcr90|msvcp90)\.dll", "vcrun2008", "Microsoft Visual C++ 2008 runtime"),
    (r"d3dx9_\d+\.dll", "d3dx9", "DirectX 9 extensions (d3dx9)"),
    (r"d3dx10(_\d+)?\.dll", "d3dx10", "DirectX 10 extensions (d3dx10)"),
    (r"d3dx11_\d+\.dll", "d3dx11_43", "DirectX 11 extensions (d3dx11)"),
    (r"d3dcompiler_43\.dll", "d3dcompiler_43", "DirectX shader compiler 43"),
    (r"d3dcompiler_47\.dll", "d3dcompiler_47", "DirectX shader compiler 47"),
    (r"(xaudio2_[0-7]|x3daudio1_\d|xapofx1_\d|xactengine\d_\d+)\.dll", "xact", "DirectX audio (XAudio2 / XACT)"),
    (r"(physxloader|physxcore|physxcudart_\d+|nxcooking)\.dll", "physx", "NVIDIA PhysX runtime"),
    (r"openal32\.dll", "openal", "OpenAL audio"),
    (r"(mscoree|mscorlib)\.dll", "dotnet48", ".NET Framework"),
    (r"msvbvm60\.dll", "vb6run", "Visual Basic 6 runtime"),
    (r"(dinput8|dinput)\.dll", "", "DirectInput"),
]


def installed_verbs(prefix_dir: Path) -> set[str]:
    """winetricks verbs installed into a game's prefix (winetricks.log in the Wine prefix)."""
    out: set[str] = set()
    for p in (prefix_dir / "pfx" / "winetricks.log", prefix_dir / "winetricks.log", prefix_dir / "wine" / "winetricks.log"):
        try:
            out |= {ln.strip() for ln in p.read_text("utf-8", "replace").splitlines() if ln.strip()}
        except OSError:
            continue
    return out


def _runtime_for(dll: str) -> tuple[str, str] | None:
    for rx, verb, what in _RUNTIME_DLLS:
        if re.fullmatch(rx, dll, re.I):
            return verb, what
    return None


# ------------------------------------------------------------------ the rules
@dataclass
class Context:
    entry: LibraryEntry
    exe: Path
    prefix_dir: Path
    run: LastRun
    engine: str | None
    engine_log_name: str
    engine_log: str
    is32: bool | None
    protons: list
    proton_name: str
    vulkan64: bool
    vulkan32: bool
    game_extra: dict
    system: object = None                 # ezgd.platform.syscheck.SystemFacts
    proton_dir: Path | None = None        # the selected Proton build (for the DLLs it ships)
    protondb: dict | None = None          # {"tier": …, "total": …} when known
    run_started: float = 0.0
    integrity: dict | None = None         # ezgd.installation.integrity report (to_dict)
    methods: list = field(default_factory=list)   # the game page's download methods (dicts)
    community: dict | None = None         # ProtonDB players' fixes for this game (protondb.for_game)
    install_dest: Path | None = None      # where the game's archive was extracted
    history: list = field(default_factory=list)   # every launch, oldest first (ezgd.library.history.Run)
    steam_ref: dict | None = None         # how Steam itself runs this game (ezgd.library.steamref.reference)
    investigated: bool = False            # the run analysed is the assistant's own diagnostic test run

    @property
    def wine(self) -> str:
        return self.run.text

    @property
    def closing(self) -> bool:
        """The engine's log ends in its normal shutdown — a crash after that happened while the game was closing."""
        tail = self.engine_log[-6000:]
        return bool(re.search(r"(GarbageCollector disposing of|Cleanup mono|Exiting via|PlayerConnection closed|"
                              r"LogExit: Exiting|LogExit: Preparing to exit|Engine is shutting down|"
                              r"ProjectSettings.*save|Deleting rendering device)", tail))

    @property
    def everything(self) -> str:
        return self.run.text + "\n" + self.engine_log


def _lines(text: str, rx: str, limit: int = 4) -> list[str]:
    out, pat = [], re.compile(rx, re.I)
    for ln in text.splitlines():
        if pat.search(ln):
            ln = ln.strip()
            if ln and ln not in out:
                out.append(ln[:300])
                if len(out) >= limit:
                    break
    return out


def r_missing_dll(c: Context) -> list[Finding]:
    out: list[Finding] = []
    pat = re.compile(r"Library ([\w.\-]+\.dll) \(which is needed by L?\"?([^\")]+)\"?\) not found", re.I)
    seen: set[str] = set()
    for m in pat.finditer(c.wine):
        dll = m.group(1).lower()
        if dll in seen:
            continue
        seen.add(dll)
        line = _lines(c.wine, re.escape(m.group(0)[:60]), 1)
        need = Path(m.group(2).replace("\\", "/")).name
        rt = _runtime_for(dll)
        if rt and rt[0]:
            verb, what = rt
            out.append(Finding(
                f"missing-dll:{dll}", f"Missing {what}",
                f"{need} needs {dll}, which is part of the {what}. Wine/Proton does not include it, so the game "
                f"cannot start. Installing it into this game's prefix (with winetricks) fixes exactly this.",
                0.92, line, Fix("winetricks", verb, f"Install {what}")))
        elif (c.exe.parent / dll).exists() or list(c.exe.parent.glob(f"**/{dll}"))[:1]:
            out.append(Finding(
                f"dll-not-loaded:{dll}", f"{dll} is there but could not be loaded",
                f"{need} needs {dll}. The file is in the game folder but could not be loaded — usually because it "
                "needs another missing library itself or is the wrong 32/64-bit version.", 0.6, line))
        else:
            out.append(Finding(
                f"missing-file:{dll}", f"Game file missing: {dll}",
                f"{need} needs {dll}, which is not in the game folder. The download or extraction is probably "
                "incomplete (or an antivirus removed it). Re-extract the archive or download the game again.",
                0.8, line, fixable=True))
    return out


_STACK_FRAME = re.compile(r"^\s*(\[\d+\]|at\s|#\d+\s)")


def _statements(text: str, rx: str, limit: int = 4) -> list[str]:
    """Like _lines, but never stack-trace frames (which only name functions, they do not report failures)."""
    return [ln for ln in _lines("\n".join(x for x in text.splitlines() if not _STACK_FRAME.match(x)), rx, limit)]


def r_steam(c: Context) -> list[Finding]:
    ev = _statements(c.everything, r"(SteamAPI_Init(\(\))? (failed|returned false)|Steamworks initiali[sz]ation "
                                   r"failed|No appID found|\[S_API FAIL\]|Steam must be running|"
                                   r"SteamAPI_RestartAppIfNecessary.*(true|1)\b)")
    starts = list(c.exe.parent.glob(f"{glob.escape(c.exe.name)}.*.STEAMSTART"))
    if starts:
        ev.append(f"{len(starts)} “{c.exe.name}.*.STEAMSTART” file(s) next to the game (it tried to restart via Steam)")
    if not ev:
        return []
    return [Finding("steam-init", "The game could not start its Steam connection",
                    "The game's Steam library refused to start. With a cracked or OnlineFix release this means "
                    "the release's own Steam files are not being used (wrong DLL loaded, or the game was started "
                    "without the release's launch script).", 0.7, ev)]


def _modules(text: str, tid: str | None = None) -> list[tuple[int, str, str]]:
    """DLLs and their load addresses (from +loaddll), for one thread when given (Wine logs several processes)."""
    mods = []
    for m in re.finditer(r'^([0-9a-f]+):trace:loaddll:\w+ Loaded L"([^"]+)" at ([0-9A-Fa-f]{8,16}): (native|builtin)',
                         text, re.M):
        if tid and m.group(1) != tid:
            continue
        mods.append((int(m.group(3), 16), m.group(2).replace("\\\\", "\\").split("\\")[-1], m.group(4)))
    return sorted(mods)


def _module_at(mods, addr: int) -> tuple[str, str] | None:
    best = None
    for base, name, kind in mods:
        if base <= addr:
            best = (name, kind)
        else:
            break
    return best


_GRAPHICS = re.compile(r"^(d3d\d+|dxgi|d3d9|ddraw|opengl32|vulkan-1|winevulkan|nvapi64|nvapi|amdxc64|"
                       r"d3dcompiler_\d+|libvkd3d.*|vkd3d.*)\.dll$", re.I)


def r_crash(c: Context) -> list[Finding]:
    m = re.search(r"^(?:([0-9a-f]+):)?.*Unhandled exception code (c[0-9a-f]{7}|[0-9a-f]{8}) flags \d+ addr "
                  r"0x([0-9a-f]+)", c.wine, re.I | re.M)
    m2 = re.search(r"Unhandled exception: (page fault on (read|write|execute) access to 0x[0-9a-f]+|[\w ]+) in "
                   r"(32|64)-bit code \(0x([0-9a-f]+)\)", c.wine, re.I)
    if not (m or m2):
        return []
    code = (m.group(2).lower() if m else "c0000005")
    addr = int((m.group(3) if m else m2.group(4)), 16)
    tid = m.group(1) if m else None
    ev = [ln for ln in _lines(c.wine, r"(Unhandled exception|NtRaiseException)", 2)]
    access = re.search(r"code=c0000005.*\n.*info\[0\]=0000000([018])", c.wine)
    mods = _modules(c.wine, tid) or _modules(c.wine)
    where = _module_at(mods, addr) if mods else None
    names = {"c0000005": "an access violation (bad memory access)", "c0000409": "a security check failure",
             "c00000fd": "a stack overflow", "c000001d": "an illegal CPU instruction",
             "e06d7363": "an unhandled C++ exception", "c0000094": "a division by zero"}
    what = names.get(code, f"exception {code}")
    if code == "c000001d":
        return [Finding("cpu-instruction", "Your CPU lacks an instruction the game needs",
                        "The game used a CPU instruction your processor does not support (often AVX/AVX2/AVX-512 "
                        "in newer games). This is not a Proton problem and cannot be fixed with settings.",
                        0.85, ev, fixable=False)]
    if where is None:
        return [Finding("crash", f"The game crashed: {what}",
                        "The game crashed during start-up. Run a diagnostic launch so the assistant can see which "
                        "part of the game crashed (module and address).", 0.45, ev)]
    mod, kind = where
    ev.append(f"crash address 0x{addr:x} is inside {mod} ({kind})")
    if c.closing:
        ev.append(f"{Path(c.engine_log_name).name} ends with the engine's normal shutdown")
        return [Finding("exit-crash", "The game crashes while closing",
                        f"The crash ({what} in {mod}) happened after the engine had started shutting down. If you "
                        "closed the game yourself this is harmless. If it closed by itself, the reason it quit comes "
                        "earlier — run a diagnostic launch.", 0.4, ev)]
    if access and access.group(1) == "8" and kind == "builtin" and mod.lower() in ("kernel32.dll", "kernelbase.dll",
                                                                                     "ntdll.dll", "user32.dll"):
        return [Finding("patched-windows-code", "The release patches Windows code in a way Wine does not support",
                        f"The crash is an attempt to run code inside Wine's {mod} that was made non-executable — "
                        "something in the game (typically an old crack, client or mod loader) patches Windows "
                        "functions in memory assuming Windows' own file layout. Wine's layout differs, so it "
                        "crashes. There is no setting that fixes this; a different release of the game usually "
                        "works.", 0.8, ev, fixable=False)]
    if _GRAPHICS.match(mod):
        dxvk = mod.lower() in ("d3d11.dll", "dxgi.dll", "d3d9.dll", "d3d10core.dll") and kind == "native"
        fix = None
        if dxvk and "wined3d" not in c.entry.compat_config.options:
            fix = Fix("enable_fix", "opt:wined3d", "Use WineD3D instead of DXVK")
        return [Finding("graphics-crash", f"The game crashed in the graphics driver layer ({mod})",
                        f"The crash ({what}) happened in {mod}, which translates the game's graphics for Linux. "
                        + ("Switching from DXVK to WineD3D avoids this layer (slower, but often starts games that "
                           "crash here)." if fix else "Updating your GPU driver or trying another Proton version "
                                                      "is the best bet."), 0.65, ev, fix)]
    game_mod = mod.lower() == c.exe.name.lower() or (c.exe.parent / mod).exists() or kind == "native"
    if game_mod:
        return [Finding("game-crash", f"The game crashed in its own code ({mod})",
                        f"The crash ({what}) happened inside the game's own {mod}. Usually something the game "
                        "expected was not there: a file (incomplete download), a setting, or a feature Wine handles "
                        "differently. See the other findings for what it was missing; if there are none, try another "
                        "Proton version (GE-Proton fixes many games).", 0.55, ev)]
    return [Finding("crash", f"The game crashed in {mod}: {what}",
                    "The crash happened in a Wine component. Trying another Proton version is the most useful next "
                    "step.", 0.5, ev)]


def r_vulkan(c: Context) -> list[Finding]:
    ev = _lines(c.everything, r"(vkCreateInstance failed|VK_ERROR_INCOMPATIBLE_DRIVER|VK_ERROR_INITIALIZATION_FAILED|"
                              r"DXVK: No adapters found|Failed to create Vulkan instance|vulkan.*not supported|"
                              r"winevulkan.*failed)")
    if not ev:
        return []
    if c.is32 and not c.vulkan32:
        return [Finding("vulkan32", "32-bit Vulkan driver missing",
                        "This is a 32-bit game, and Proton draws it through Vulkan — but your system has no 32-bit "
                        "Vulkan driver installed, so Vulkan cannot start. Install your GPU's 32-bit Vulkan driver "
                        "(e.g. lib32-vulkan-radeon, lib32-vulkan-intel or lib32-nvidia-utils).", 0.85, ev,
                        Fix("system", "lib32-vulkan", "Open System check"))]
    fix = None if "wined3d" in c.entry.compat_config.options else Fix("enable_fix", "opt:wined3d",
                                                                        "Use WineD3D (OpenGL) instead")
    return [Finding("vulkan", "Vulkan could not start", "Proton's DirectX translation needs Vulkan and it failed "
                    "to start. Check that your GPU driver (and its Vulkan package) is installed; as a workaround "
                    "WineD3D draws with OpenGL instead.", 0.75, ev, fix)]


def r_dx12(c: Context) -> list[Finding]:
    ev = _lines(c.everything, r"(D3D12CreateDevice|vkd3d.*(failed|error|not supported)|D3D12 is not supported|"
                              r"DX12.*(not supported|failed)|rendering_device_driver_d3d12.*ERR)")
    if not ev:
        return []
    if c.engine == "Unreal Engine" and "-dx11" not in (c.entry.launch_args or ""):
        return [Finding("dx12-unreal", "DirectX 12 does not start — use DirectX 11",
                        "This Unreal Engine game uses DirectX 12, which failed to start under Proton (vkd3d). "
                        "Unreal games accept -dx11 to use DirectX 11 instead, which runs through the more mature "
                        "DXVK.", 0.8, ev, Fix("add_args", "-dx11", "Start with DirectX 11"))]
    if c.engine == "Unity" and "-force-d3d11" not in (c.entry.launch_args or ""):
        return [Finding("dx12-unity", "DirectX 12 does not start — use DirectX 11",
                        "This Unity game is using DirectX 12, which failed under Proton. -force-d3d11 makes Unity "
                        "use DirectX 11 instead.", 0.75, ev, Fix("add_args", "-force-d3d11", "Start with DirectX 11"))]
    return [Finding("dx12", "DirectX 12 does not start", "The game's DirectX 12 renderer failed to start under "
                    "Proton (vkd3d-proton). Check for a DirectX 11 option in the game's launcher or settings, or "
                    "try GE-Proton.", 0.6, ev)]


def r_anticheat(c: Context) -> list[Finding]:
    ev = _lines(c.everything, r"(EasyAntiCheat|EAC.*(error|failed|not installed)|BattlEye|BEService|"
                              r"anti-?cheat.*(error|fail))")
    if not ev:
        return []
    return [Finding("anticheat", "Anti-cheat stops the game", "The game's anti-cheat (EasyAntiCheat/BattlEye) is "
                    "refusing to run. Anti-cheat only works under Proton when the game's developer enabled it for "
                    "Linux, and never for modified releases. There is no setting that fixes this; a release with "
                    "the anti-cheat removed (as OnlineFix does) is needed.", 0.8, ev, fixable=False)]


def r_dotnet(c: Context) -> list[Finding]:
    xna = _lines(c.everything, r"Could not load (file or )?assembly '?Microsoft\.Xna\.Framework")
    if xna:
        return [Finding("xna", "Missing XNA Framework", "The game is built on Microsoft XNA, which is not in the "
                        "prefix. Installing XNA 4.0 (winetricks xna40) fixes this.", 0.9, xna,
                        Fix("winetricks", "xna40", "Install XNA Framework 4.0"))]
    ev = _lines(c.everything, r"(wine-mono|mscoree.*(not|fail)|CLR error|System\.(IO\.FileNotFoundException|"
                              r"TypeLoadException)|Could not load file or assembly)")
    if not ev:
        return []
    own = _bundled_runtime(c)
    if own:
        # Godot C#, .NET 5+ and Unity games bring their own runtime: the .NET Framework is not what is missing
        # (Slay the Spire 2 was offered dotnet48, which then failed to install — 2026-10-13)
        return [Finding("dotnet-bundled", "A part of the game's own .NET runtime could not be loaded",
                        f"The game ships its own {own} (it does not use the Windows .NET Framework), so installing "
                        ".NET Framework would not help. A file of the game is probably missing or damaged — repair "
                        "or re-extract the game — or the Proton version cannot run it (try another Proton).", 0.6,
                        ev)]
    return [Finding("dotnet", "A .NET component is missing", "The game uses .NET and a part of it could not be "
                    "loaded. Installing the .NET Framework into the prefix usually fixes it.", 0.7, ev,
                    Fix("winetricks", "dotnet48", "Install .NET Framework 4.8"))]


def _bundled_runtime(c: Context) -> str:
    """The .NET / Mono runtime a game brings along ("" when it relies on the system's .NET Framework)."""
    root = Path(c.entry.install_path or c.exe.parent)
    for d in {c.exe.parent, root}:
        try:
            if (d / "MonoBleedingEdge").is_dir() or any(d.glob("*_Data/Managed/UnityEngine.dll")):
                return "Mono runtime (Unity)"
            for name in ("coreclr.dll", "hostfxr.dll"):
                if (d / name).exists() or any(d.glob(f"*/{name}")):
                    return ".NET runtime (" + ("Godot C#" if c.engine == "Godot" or any(d.glob("*.pck")) else
                                               ".NET 5 or newer") + ")"
        except OSError:
            continue
    return ""


def r_media(c: Context) -> list[Finding]:
    ev = _lines(c.everything, r"(winegstreamer.*(error|fail|missing)|mfplat.*(fail|not implemented)|"
                              r"MFCreateSourceReader|No decoder|could not find a decoder)")
    if not ev:
        return []
    ge = next((p for p in c.protons if "ge" in p.name.lower() or "cachyos" in p.name.lower()), None)
    using_ge = "ge" in c.proton_name.lower() or "cachyos" in c.proton_name.lower()
    fix = Fix("proton", ge.name, f"Use {ge.name}") if ge and not using_ge else None
    return [Finding("media", "Videos cannot be played", "The game's videos (Media Foundation) fail to decode. "
                    "Valve's Proton lacks some codecs; GE-Proton/Proton-CachyOS include them. Games often hang or "
                    "crash at an intro video because of this.", 0.7, ev, fix)]


def r_prefix(c: Context) -> list[Finding]:
    ev = _lines(c.wine, r"Prefix has an invalid version|Upgrading prefix from .* to |Downgrading prefix")
    if not any("invalid version" in e.lower() or "downgrad" in e.lower() for e in ev):
        return []
    return [Finding("prefix-version", "The game's prefix was made by a different Proton",
                    "This prefix was created by another (newer) Proton version than the one now selected. Mixing "
                    "versions can break games. Either select the Proton it was made with, or rebuild the prefix: the "
                    "old one is kept, and your saves can be copied into the new one afterwards.", 0.6, ev,
                    Fix("reset_prefix", "", "Rebuild the prefix (the old one is kept)"))]


def r_quick_exit(c: Context) -> list[Finding]:
    if c.run.seconds is None or c.run.seconds > 8 or c.run.exit_code not in (0, None):
        return []
    root = Path(c.entry.install_path or c.exe.parent)
    cands = rank(find_executables(root), root, c.entry.title)
    better = next((x for x in cands if x.path != c.exe and x.kind in ("game", "launch script") and x.score >
                   next((y.score for y in cands if y.path == c.exe), -999)), None)
    if better is None and not re.search(r"(launcher|setup|config|update)", c.exe.stem, re.I):
        return []
    ev = [f"the last run ended normally after {c.run.seconds}s without an error"]
    return [Finding("wrong-exe", "The game closes right away — probably the wrong executable",
                    f"{c.exe.name} exited cleanly after {c.run.seconds}s, which is what launchers, setup tools and "
                    "stubs do when they are not the game itself."
                    + (f" {better.path.name} looks like the actual game ({better.reason})." if better else ""),
                    0.6, ev, Fix("set_executable", str(better.path), f"Use {better.path.name}") if better else None)]


def r_native(c: Context) -> list[Finding]:
    ev = _lines(c.wine, r"error while loading shared libraries: ([\w.+-]+)")
    if not ev:
        return []
    lib = re.search(r"error while loading shared libraries: ([\w.+-]+)", "\n".join(ev)).group(1)
    return [Finding("native-lib", f"Missing system library: {lib}",
                    f"This native Linux game needs {lib}, which is not installed. Install the package that provides "
                    "it with your distribution's package manager (search for the file name).", 0.85, ev,
                    fixable=False)]


def r_advisor(c: Context) -> list[Finding]:
    """The existing file-based checks (launch scripts, OnlineFix DLLs, Godot renderer, launch options)."""
    out = []
    for s in advisor.suggestions(c.entry, c.prefix_dir, c.game_extra):
        fix = None
        ch = s.changes
        if "enable_fix" in ch:
            fix = Fix("enable_fix", ch["enable_fix"], "Apply")
        elif "set_executable" in ch:
            fix = Fix("set_executable", ch["set_executable"], f"Use {Path(ch['set_executable']).name}")
        elif "launch_args" in ch:
            fix = Fix("launch_args", ch["launch_args"], "Fix the launch options")
        conf = 0.75 if s.severity == "warning" else 0.5
        out.append(Finding(f"advisor:{s.key}", s.title, s.detail, conf, [], fix))
    return out


# ------------------------------------------------- deeper checks (no run needed)
_BUILTINS: dict[str, frozenset[str]] = {}


def proton_dlls(proton_dir: Path | None, bits: int) -> frozenset[str]:
    """DLL names the selected Proton provides for 32- or 64-bit programs (Wine builtins, DXVK, vkd3d …)."""
    if proton_dir is None:
        return frozenset()
    key = f"{proton_dir}:{bits}"
    if key not in _BUILTINS:
        arch = "x86_64-windows" if bits == 64 else "i386-windows"
        sub = "x64" if bits == 64 else "x32"
        names: set[str] = set()
        for d in (proton_dir / "files" / "lib" / "wine" / arch, proton_dir / "files" / "lib64" / "wine" / arch,
                  proton_dir / "files" / "lib" / "wine" / "dxvk" / arch, proton_dir / "files" / "lib" / "wine" /
                  "vkd3d-proton" / arch, proton_dir / "files" / "lib" / "wine" / "dxvk" / sub,
                  proton_dir / "files" / "lib" / "wine" / "nvapi" / arch):
            if d.is_dir():
                names.update(p.name.lower() for p in d.iterdir() if p.suffix.lower() in (".dll", ".drv", ".exe"))
        _BUILTINS[key] = frozenset(names)
    return _BUILTINS[key]


def r_imports(c: Context) -> list[Finding]:
    """Predict "DLL not found" from the import tables of the game's exe and the DLLs it loads from its folder."""
    from ezgd.platform.pe import read_pe
    if not is_windows_binary(c.exe) or c.proton_dir is None:
        return []
    root = c.exe.parent
    main = read_pe(c.exe)
    if main is None:
        return []
    builtins = proton_dlls(c.proton_dir, main.bits)
    if not builtins:
        return []
    sysdir = c.prefix_dir / "pfx" / "drive_c" / "windows" / ("system32" if main.bits == 64 else "syswow64")
    native = {p.name.lower() for p in sysdir.glob("*.dll")} if sysdir.is_dir() else set()
    local = {p.name.lower(): p for p in root.glob("*.dll")}
    missing: dict[str, str] = {}
    mismatch: list[str] = []
    todo, done = [(c.exe, main)], set()
    while todo and len(done) < 60:
        path, info = todo.pop()
        done.add(path.name.lower())
        for dll in info.imports:                 # delay-loaded DLLs are optional: not counted as missing
            if dll.startswith(("api-ms-win-", "ext-ms-")) or dll in builtins or dll in native:
                continue
            if dll in local:
                if dll not in done:
                    sub = read_pe(local[dll])
                    if sub is not None:
                        if sub.bits != main.bits:
                            mismatch.append(f"{local[dll].name} is {sub.bits}-bit but {c.exe.name} is {main.bits}-bit")
                        else:
                            todo.append((local[dll], sub))
                continue
            missing.setdefault(dll, path.name)
    out: list[Finding] = []
    for dll, needer in missing.items():
        if f"missing-dll:{dll}" in c.wine.lower():
            continue
        rt = _runtime_for(dll)
        if rt and rt[0]:
            verb, what = rt
            out.append(Finding(f"predicted-dll:{dll}", f"{what} will be missing",
                               f"{needer} needs {dll} (from its import table). Neither your Proton nor the game "
                               f"folder provides it, so the game cannot start until the {what} is installed into "
                               "its prefix.", 0.85, [f"{needer} imports {dll}"],
                               Fix("winetricks", verb, f"Install {what}")))
        else:
            out.append(Finding(f"predicted-file:{dll}", f"{dll} is missing",
                               f"{needer} needs {dll} (from its import table), but it is not in the game folder and "
                               "Proton does not provide it. The game cannot start without it — the download or "
                               "extraction is probably incomplete, or a fix archive was not extracted.",
                               0.75, [f"{needer} imports {dll}"]))
    for m in mismatch[:3]:
        out.append(Finding("bitness", "A 32/64-bit mix-up in the game folder",
                           f"{m}. Windows refuses to load it (error “bad image” / 0xC000007B). This usually comes "
                           "from a mod or fix made for the other version of the game.", 0.8, [m]))
    return out


def r_unimplemented(c: Context) -> list[Finding]:
    m = re.search(r"unimplemented function ([\w.\-]+\.dll)\.([^\s,]+)", c.wine, re.I)
    if not m:
        return []
    dll, fn = m.group(1).lower(), m.group(2)
    ev = _lines(c.wine, r"unimplemented function", 3)
    if c.closing:
        ev.append(f"{Path(c.engine_log_name).name} ends with the engine's normal shutdown")
        return [Finding(f"exit-unimplemented:{dll}", "The game crashes while closing (missing Wine function)",
                        f"When the game was shutting down it called {fn} in {dll}, which this Proton does not "
                        "implement, so Wine aborted it. If you quit the game yourself this is harmless (only the exit "
                        "code is wrong). If the game closed by itself, something else made it quit first — run a "
                        "diagnostic launch to see what.", 0.4, ev)]
    rt = _runtime_for(dll)
    if rt and rt[0]:
        verb, what = rt
        return [Finding(f"unimplemented:{dll}", f"Wine's {dll} lacks a function the game needs",
                        f"The game called {fn} in {dll}. Proton has its own version of the {what}, and it does not "
                        f"implement that function, so the game aborts. Installing Microsoft's {what} into the prefix "
                        "replaces Wine's version.", 0.9, ev, Fix("winetricks", verb, f"Install the {what}"))]
    return [Finding(f"unimplemented:{dll}", f"Wine does not implement {dll}.{fn}",
                    f"The game called {fn} in {dll}, which Wine/Proton has not implemented yet. A newer Proton "
                    "(Proton Experimental or GE-Proton) may have it; otherwise the game cannot run until Wine adds "
                    "it.", 0.8, ev)]


def r_minidump(c: Context) -> list[Finding]:
    from ezgd.library.minidump import find_dumps, read_minidump
    since = c.run_started - 5 if c.run_started else time.time() - 7 * 86400
    for p in find_dumps(c.exe.parent, c.prefix_dir, since):
        d = read_minidump(p)
        if d is None or d.code is None:
            continue
        mod = d.module
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.mtime))
        ev = [f"{p.name} ({when}): exception 0x{d.code:08X} at 0x{d.address:X}"
              + (f" = {mod.name}+0x{d.offset:X}" if mod else "")]
        names = {0xC0000005: "an access violation", 0xC0000409: "a security check failure",
                 0xC00000FD: "a stack overflow", 0xC000001D: "an illegal CPU instruction",
                 0xE06D7363: "an unhandled C++ exception", 0x80000003: "a breakpoint (deliberate abort)"}
        what = names.get(d.code, f"exception 0x{d.code:08X}")
        if d.code == 0xC000001D:
            return [Finding("dump-cpu", "Your CPU lacks an instruction the game needs",
                            "The crash dump shows an illegal-instruction crash: the game uses a CPU feature (often "
                            "AVX2/AVX-512) your processor does not have. No setting can fix this.", 0.85, ev,
                            fixable=False)]
        if mod is None:
            return [Finding("dump", f"The game wrote a crash dump: {what}",
                            "The crash happened outside any loaded module (often a jump to a bad address). A "
                            "diagnostic launch shows which component caused it.", 0.5, ev)]
        if _GRAPHICS.match(mod.name):
            fix = None if "wined3d" in c.entry.compat_config.options else                 Fix("enable_fix", "opt:wined3d", "Use WineD3D instead of DXVK")
            return [Finding("dump-graphics", f"Crash dump: {what} in {mod.name}",
                            f"The game crashed inside {mod.name}, the graphics layer. Switching to WineD3D, a newer "
                            "Proton or a GPU driver update are the realistic fixes.", 0.65, ev, fix)]
        if d.access == 8 and mod.name.lower() in ("kernel32.dll", "kernelbase.dll", "ntdll.dll"):
            return [Finding("dump-patched", "The release patches Windows code in a way Wine does not support",
                            f"The dump shows code being run from a non-executable part of {mod.name} — an old crack or "
                            "client patching Windows functions. No setting fixes this.", 0.8, ev, fixable=False)]
        return [Finding("dump-game", f"Crash dump: {what} in {mod.name}",
                        f"The game crashed in {mod.name}. Look at the other findings for what it was missing; if "
                        "there are none, another Proton version is the next thing to try (“Try fixes for me”).",
                        0.6, ev)]
    return []


def r_system(c: Context) -> list[Finding]:
    from ezgd.platform import syscheck
    s = c.system
    if s is None:
        return []
    out = []
    from ezgd.core.fsutil import format_bytes
    if s.fs_prefix in syscheck.FRAGILE_FS:
        out.append(Finding("fs-prefix", f"The Proton prefix is on a {syscheck.FRAGILE_FS[s.fs_prefix]} drive",
                           "Proton needs a Linux filesystem for its prefix (symlinks, permissions, case handling). "
                           "On NTFS/exFAT/FAT the prefix breaks in confusing ways. Move the app's data folder (and "
                           "so the prefixes) to a Linux-formatted drive (ext4, btrfs, xfs).", 0.85,
                           [f"{c.prefix_dir} is on {s.fs_prefix}"]))
    if s.fs_game in syscheck.FRAGILE_FS:
        out.append(Finding("fs-game", f"The game is on a {syscheck.FRAGILE_FS[s.fs_game]} drive",
                           "Games on NTFS/exFAT drives often fail under Proton (no case-insensitivity support, "
                           "permission and symlink problems, slow loading with fuseblk). Moving the game to a "
                           "Linux-formatted drive is the reliable fix.", 0.6, [f"{c.exe.parent} is on {s.fs_game}"]))
    low = [(w, f) for w, f in (("game", s.free_game), ("prefix", s.free_prefix)) if f is not None and f < 2 << 30]
    for where, free in low:
        out.append(Finding(f"disk-{where}", "The drive is almost full",
                           f"Only {format_bytes(free)} is free on the {where}'s drive. Games and Proton need space "
                           "for shader caches, saves and the prefix; with less than a few GB they crash or fail to "
                           "save.", 0.7, [f"{format_bytes(free)} free"]))
    crashy = bool(re.search(r"(Unhandled exception|c0000005|out of memory|Fatal error|LowLevelFatalError)",
                            c.everything, re.I))
    if s.max_map_count is not None and s.max_map_count < syscheck.MAP_COUNT_RECOMMENDED and             (crashy or c.engine == "Unreal Engine"):
        out.append(Finding("map-count", "The memory-map limit is too low for big games",
                           f"vm.max_map_count is {s.max_map_count:,}. Large games (especially Unreal Engine ones) "
                           "need more and crash while loading when they hit the limit; SteamOS and recent distributions "
                           "use 2147483642. Raising it needs administrator rights, so the app cannot do it for you.",
                           0.7 if crashy else 0.45, [f"vm.max_map_count = {s.max_map_count}"],
                           Fix("command", "sudo sysctl -w vm.max_map_count=2147483642 && echo "
                               "'vm.max_map_count = 2147483642' | sudo tee /etc/sysctl.d/80-gamecompat.conf",
                               "Copy the command to raise it")))
    if "too many open files" in c.everything.lower():
        out.append(Finding("nofile", "The open-file limit is too low", "The game hit the open-files limit "
                           f"(hard limit {s.nofile_hard or 'unknown'}). Esync needs at least 524288. Raising it needs "
                           "a system setting (DefaultLimitNOFILE in /etc/systemd/system.conf).", 0.8,
                           _lines(c.everything, "too many open files", 2), fixable=False))
    return out


def r_protondb(c: Context) -> list[Finding]:
    p = c.protondb or {}
    if (p.get("tier") or "").lower() != "borked":
        return []
    return [Finding("protondb-borked", "ProtonDB: most players say this game does not run",
                    f"ProtonDB rates it Borked ({p.get('total', '?')} reports). That usually means a missing Wine "
                    "feature or anti-cheat, not a setting. The checks above still apply, but a fix is unlikely.",
                    0.55, [f"ProtonDB tier: {p.get('tier')}, {p.get('total')} reports"], fixable=False)]


def r_old_prefix(c: Context) -> list[Finding]:
    from ezgd.platform.proton import prefix_ready
    olds = sorted(c.prefix_dir.parent.glob(f"{c.prefix_dir.name}.old-*"), reverse=True)
    if not olds or not prefix_ready(c.prefix_dir):
        return []
    return [Finding("old-prefix", "Saves can be copied from the previous prefix",
                    f"The prefix was rebuilt; the old one is kept as {olds[0].name}. Copying the saved games and "
                    "settings (AppData, Documents, Saved Games) into the new prefix brings your progress back.",
                    0.5, [str(olds[0])], Fix("restore_saves", str(olds[0]), "Copy my saves into the new prefix"))]


# ------------------------------------------------------------- release problems
_FIX_NAME = re.compile(r"(fix[\s._-]*repair|fix[\s._-]*only|[\s._-]fix[\s._-]|hot[\s._-]?fix|\bupdate\b|\bpatch\b|"
                       r"\bcrack\b|steamfix|_fix\b)", re.I)
_CRACK_DIRS = re.compile(r"^(crack|cracked|fix|fixes|fix[ _-]?repair|update|patch|codex|rune|plaza|skidrow|tenoke|"
                         r"empress|goldberg|onlinefix|steamworks[ _-]?fix|_?crack[ _-]?fix)$", re.I)


def _steam_trouble(c: Context) -> list[str]:
    ev = _statements(c.everything, r"(SteamAPI_Init(\(\))? (failed|returned false)|Steamworks initiali[sz]ation "
                                   r"failed|No appID found|\[S_API FAIL\]|Steam must be running|"
                                   r"SteamAPI_RestartAppIfNecessary.*(true|1)\b)", 3)
    starts = list(c.exe.parent.glob(f"{glob.escape(c.exe.name)}.*.STEAMSTART"))
    if starts:
        ev.append(f"{len(starts)} .STEAMSTART file(s): the game asked Steam to restart it")
    return ev


def r_emulator(c: Context) -> list[Finding]:
    import json as _json
    from ezgd.library import emulators as em
    if not is_windows_binary(c.exe):
        return []
    trouble = _steam_trouble(c)
    app_id = c.entry.steam_app_id
    needs = em.needs_steam_dll(c.exe)
    here, others = em.steam_dll_for(c.exe, Path(c.entry.install_path) if c.entry.install_path else None)
    out: list[Finding] = []
    if needs and here is None and others:
        src = next((o for o in others if o.name == needs), others[0])
        pairs = [[str(src), str(c.exe.parent / src.name)]]
        for comp in em.COMPANIONS:
            if (src.parent / comp).exists() and not (c.exe.parent / comp).exists():
                pairs.append([str(src.parent / comp), str(c.exe.parent / comp)])
        for d in em.COMPANION_DIRS:
            if (src.parent / d).is_dir() and not (c.exe.parent / d).exists():
                pairs.append([str(src.parent / d), str(c.exe.parent / d)])
        out.append(Finding("emu-location", f"{needs} is not next to {c.exe.name}",
                           f"{c.exe.name} loads {needs} from its own folder, but the release put it in "
                           f"{src.parent.relative_to(Path(c.entry.install_path or c.exe.parent)) if c.entry.install_path else src.parent}. "
                           f"Copying it (with its {len(pairs) - 1} config file(s)) next to the game fixes the Steam "
                           "start-up.", 0.85, [f"{c.exe.name} imports {needs}", f"found at {src}"],
                           Fix("copy_files", _json.dumps(pairs), f"Copy {needs} next to the game")))
        return out
    if here is None:
        return out
    setup = em.identify(here)
    boost = 0.1 if trouble else 0.0
    ev = [f"{here.name}: {setup.name}"] + trouble
    if setup.kind == "valve":
        appid_txt = c.exe.parent / "steam_appid.txt"
        if not appid_txt.exists() and (trouble or c.run.found):
            if app_id:
                out.append(Finding("emu-appid-txt", "steam_appid.txt is missing",
                                   "The release uses Valve's own Steam API (a DRM-free game). Without a "
                                   "steam_appid.txt next to the game, the Steam API asks the Steam client to restart "
                                   f"the game and closes it. Writing steam_appid.txt with the game's App ID ({app_id}) "
                                   "stops that.", 0.75 + boost, ev,
                                   Fix("write_file", _json.dumps({"path": str(appid_txt), "content": f"{app_id}\n"}),
                                       "Write steam_appid.txt")))
            else:
                out.append(Finding("emu-appid-unknown", "steam_appid.txt is missing and the App ID is unknown",
                                   "The release uses Valve's own Steam API and needs a steam_appid.txt with the "
                                   "game's Steam App ID. Set the App ID in Properties → Compatibility (Look up) and "
                                   "run Diagnose again — the assistant then writes the file.", 0.6 + boost, ev))
        return out
    key = {"codex": "AppId", "rune": "AppId", "ali213": "AppID", "sse": "AppId", "creamapi": "appid"}.get(setup.kind)
    if setup.kind == "goldberg":
        if app_id and setup.app_id != app_id:
            target = here.parent / "steam_settings" / "steam_appid.txt"
            out.append(Finding("emu-goldberg-appid", "The Goldberg emulator has no (or the wrong) App ID",
                               f"Goldberg reads the App ID from steam_settings/steam_appid.txt; it is "
                               f"{'missing' if setup.app_id is None else setup.app_id}, the game's is {app_id}. "
                               "Without the right App ID the emulator cannot start the game's Steam features.",
                               0.65 + boost, ev, Fix("write_file", _json.dumps({"path": str(target),
                                                                                "content": f"{app_id}\n"}),
                                                     "Write the App ID for Goldberg")))
    elif key and setup.config is not None and setup.config.exists():
        if app_id and setup.app_id != app_id and (setup.app_id in (None, 0) or trouble):
            out.append(Finding(f"emu-{setup.kind}-appid", f"The {setup.name} is set up with the wrong App ID",
                               f"{setup.config.name} has {key}={setup.app_id if setup.app_id is not None else '(empty)'}"
                               f" but the game's Steam App ID is {app_id}. Setting it correctly (a backup of the file "
                               "is kept) fixes Steam start-up errors.", 0.65 + boost, ev,
                               Fix("edit", _json.dumps([{"path": str(setup.config), "kind": "keyval", "key": key,
                                                         "value": str(app_id)}]), f"Set {key}={app_id}")))
    if setup.kind == "creamapi" and not any((here.parent / n).exists() for n in ("steam_api_o.dll", "steam_api64_o.dll")):
        out.append(Finding("emu-cream-original", "CreamAPI's original Steam DLL is missing",
                           "CreamAPI (a DLC unlocker) loads Valve's original DLL renamed to steam_api(64)_o.dll, "
                           "which is not in the folder — the release is incomplete. It also needs the Steam client "
                           "running and signed in.", 0.7, ev, fixable=False))
    return out


def _archive_applied(archive: Path, dest: Path) -> bool | None:
    from ezgd.installation import archives as ar
    fmt = ar.detect_format(archive)
    if fmt is None:
        return None
    try:
        members = [m for m in ar.list_members(archive, fmt) if not (m.is_dir or m.is_link or m.is_special)]
    except ar.ArchiveError:
        return None
    if not members:
        return None
    hits = 0
    for m in members:
        p = dest / ar._norm(m.name)
        if p.exists() and (m.size is None or p.stat().st_size == m.size):
            hits += 1
    return hits == len(members)


def _fix_applied_before_run(c: Context) -> bool:
    """A fix archive was put into the game by the app (its .ezgd-backup-<time> folder) before the run that failed —
    applying the same fix again cannot help (Little Nightmares III got the Fix Repair twice, 2026-10-12)."""
    root = Path(c.entry.install_path or c.exe.parent)
    newest = 0.0
    for d in [*root.glob(".ezgd-backup-*"), *c.exe.parent.glob(".ezgd-backup-*")]:
        try:
            if d.is_dir():
                newest = max(newest, d.stat().st_mtime)
        except OSError:
            continue
    has_fix = any((f / n).exists() for f in {root, c.exe.parent} for n in ("OnlineFix64.dll", "OnlineFix.ini"))
    if not has_fix:
        try:
            has_fix = any(True for _ in root.glob("**/OnlineFix64.dll"))
        except OSError:
            has_fix = False
    return bool(newest and has_fix and c.run_started and newest < c.run_started)


def r_fix_archive(c: Context) -> list[Finding]:
    import json as _json
    from ezgd.installation import archives as ar
    out: list[Finding] = []
    dest = c.install_dest or (Path(c.entry.install_path) if c.entry.install_path else None)
    if dest is None or not dest.exists():
        return out
    found: list[Path] = []
    if c.entry.download_path:
        dp = Path(c.entry.download_path)
        if dp.is_dir():
            found += ar.find_archives(dp, 2)
        elif dp.parent.is_dir():               # its own file and ones next to it named after the same game
            def key(t: str) -> str:
                return re.sub(r"[^a-z0-9]", "", t.lower())
            own = key(re.split(r"[._\- ]", dp.name, 1)[0]) or "\0"
            title = key(c.entry.title) or "\0"
            for a in ar.find_archives(dp.parent, 1):
                k = key(a.name)
                if a == dp or k.startswith(own) or k.startswith(title):
                    found.append(a)
    if dest.is_dir():
        found += ar.find_archives(dest, 2)
    seen = set()
    for _place in (0,):
        for a in found:
            if a in seen or not _FIX_NAME.search(a.name):
                continue
            seen.add(a)
            applied = _archive_applied(a, dest)
            if applied is False:
                out.append(Finding(f"fix-archive:{a.name}", f"The release's fix archive was not applied: {a.name}",
                                   f"{a.name} contains files for the game that are not in its folder yet (fix or "
                                   "update archives are extracted over the game, as the release's instructions say; "
                                   "the app does not do that automatically). Applying it keeps a backup of every file "
                                   "it replaces.", 0.75 if "repair" in a.name.lower() else 0.65, [str(a)],
                                   Fix("apply_archive", _json.dumps({"archive": str(a), "dest": str(dest)}),
                                       f"Apply {a.name}")))
    # a Crack / Fix / Update *folder* inside the release that was meant to be copied over the game
    root = Path(c.entry.install_path or dest)
    readme = " ".join(_tail(p, 64 * 1024) for p in list(root.glob("*.txt"))[:10]).lower()
    for d in [p for p in root.glob("*") if p.is_dir() and _CRACK_DIRS.match(p.name)] +             [p for p in root.glob("*/*") if p.is_dir() and _CRACK_DIRS.match(p.name)]:
        files = [f for f in d.rglob("*") if f.is_file()]
        if not files or len(files) > 400:
            continue
        targets: dict[Path, int] = {}
        differ = 0
        for f in files:
            twin = next((t for t in root.rglob(f.name) if t.is_file() and d not in t.parents), None)
            if twin is None:
                continue
            targets[twin.parent] = targets.get(twin.parent, 0) + 1
            try:
                if twin.stat().st_size != f.stat().st_size or twin.read_bytes()[:65536] != f.read_bytes()[:65536]:
                    differ += 1
            except OSError:
                continue
        if not targets or not differ:
            continue
        dst = max(targets, key=targets.get)
        told = f"{d.name.lower()} folder" in readme or "copy the contents" in readme
        out.append(Finding(f"crack-folder:{d.name}", f"The release's “{d.name}” folder was not copied into the game",
                           f"{d.relative_to(root)} holds {len(files)} file(s) meant to replace the game's own "
                           f"({differ} still differ from the installed ones)"
                           + (" and the release's readme says to copy it" if told else "") +
                           f". Copying it into {dst.relative_to(root) if dst != root else 'the game folder'} keeps a "
                           "backup of every file it replaces.", 0.85 if told else 0.7,
                           [f"{differ} file(s) in {d.name} differ from the game's"],
                           Fix("copy_folder", _json.dumps({"src": str(d), "dst": str(dst)}),
                               f"Copy “{d.name}” into the game")))
    # OnlineFix publishes a separate "Fix Repair" (fix files only) download for when a game does not start
    if c.entry.source == "onlinefix" and not any(f.key.startswith("fix-archive:") for f in out):
        idx = next((i for i, m in enumerate(c.methods) if "fix files only" in (m.get("label") or "").lower()
                    and m.get("supported")), None)
        failing = bool(_steam_trouble(c)) or re.search(r"Unhandled exception", c.wine) or \
            (c.run.exit_code not in (None, 0) and (c.run.seconds or 99) < 30)
        if idx is not None and failing and not _fix_applied_before_run(c):
            out.append(Finding("onlinefix-fix-repair", "Apply OnlineFix's “Fix Repair”",
                               "OnlineFix publishes a small “Fix Repair” archive for this game for exactly this case: "
                               "the game does not start or loses its online fix. The app can download it (in-app) and "
                               "extract it over the game, keeping backups of what it replaces.", 0.6,
                               [c.methods[idx].get("label", "")[:120]],
                               Fix("download_fix", str(idx), "Download and apply the Fix Repair")))
    return out


def r_integrity(c: Context) -> list[Finding]:
    r = c.integrity
    if not r:
        return []
    out = []
    if r.get("missing") or r.get("damaged"):
        n = r["missing"] + r["damaged"]
        repairable = r.get("repairable", 0)
        out.append(Finding("integrity", f"{n} game file(s) are missing or damaged",
                           f"Compared with the archive it came from ({', '.join(r['archives'][:2])}), "
                           f"{r['missing']} file(s) are missing and {r['damaged']} have the wrong size — an incomplete "
                           "extraction or a file deleted later. "
                           + (f"All {repairable} can be restored from the archive (only those files are extracted)."
                              if repairable == n else
                              f"{repairable} can be restored from the archive; for the rest the archive is gone — "
                              "download the game again."), 0.9, r.get("examples", []),
                           Fix("repair_files", "", f"Repair {repairable} file(s)") if repairable else None))
    elif r.get("unverifiable") and not r.get("archives"):
        out.append(Finding("integrity-unknown", "The game files cannot be checked",
                           "The archive the game was extracted from has been deleted, so the app cannot compare the "
                           "installed files with it. If nothing else explains the problem, download the game again.",
                           0.25, [", ".join(dict.fromkeys(r["unverifiable"][:6]))]))
    return out


def _latest(c: Context):
    """The history entry of the run being analysed (None when the log has no matching launch)."""
    if not c.history:
        return None
    last = c.history[-1]
    return last if last.started == c.run.started else None


def r_regression(c: Context) -> list[Finding]:
    """The game worked before and fails now: what changed in between is the prime suspect."""
    import json as _json
    from ezgd.library import history
    bad = _latest(c)
    if bad is None or not bad.failed:
        return []
    good = history.last_worked(c.history, bad)
    if good is None:
        return []
    when = good.started[:16]
    diffs = [d for d in history.changes(good, bad) if d[0] != "kernel" or len(history.changes(good, bad)) == 1]
    fails_since = sum(1 for r in c.history[c.history.index(good) + 1:] if r.failed)
    ev = [f"worked {when} ({good.seconds}s, exit code {good.exit_code})",
          f"{fails_since} failed start(s) since; the last ended after {bad.seconds}s (exit code {bad.exit_code})"]
    if not diffs:
        outside = []
        exe = Path(c.entry.executable or "")
        try:
            if exe.is_file() and exe.stat().st_mtime > good.at + 60:
                outside.append("the game's executable was replaced (an update or a re-download) on "
                               + time.strftime("%Y-%m-%d %H:%M", time.localtime(exe.stat().st_mtime)))
        except OSError:
            pass
        if c.proton_dir:
            try:
                if Path(c.proton_dir).stat().st_mtime > good.at + 60:
                    outside.append(f"{c.proton_name or 'the Proton build'} was updated in place since then")
            except OSError:
                pass
        return [Finding("regression-outside", f"It worked on {when} with the same setup",
                        "Nothing in the app's launch setup changed since the game last worked, so something "
                        "outside it did. " + ("Found: " + "; ".join(outside) + ". " if outside else
                                              "Typical causes: a game update, a Proton or graphics-driver update, "
                                              "or a changed save/config file. ")
                        + "Trying another Proton version is the quickest test.", 0.55 if outside else 0.4,
                        ev + outside)]
    lines = []
    for key, what, then, now in diffs:
        fmt = (lambda v: ", ".join(v) if isinstance(v, list) else ("on" if v is True else "off" if v is False
                                                                     else str(v or "none")))
        lines.append(f"{what}: {fmt(then)} → {fmt(now)}")
    ev += lines
    restore: dict = {}
    s = good.setup or {}
    for key, _what, then, _now in diffs:
        if key == "proton" and then and any(getattr(p, "name", "") == then for p in c.protons):
            restore["proton_version"] = then
        elif key == "launch_args" and "launch_args" in s:
            restore["launch_args"] = s["launch_args"]
        elif key == "executable" and s.get("executable") and Path(s["executable"]).is_file():
            restore["executable"] = s["executable"]
        elif key in ("fixes", "options", "dll_overrides", "use_runtime", "protonfixes") and key in s:
            cfg = restore.setdefault("compat", c.entry.compat_config.to_dict())
            cfg[key] = s[key]
        elif key == "overlay" and _now in ("full", "friends list") and \
                "no_steam_overlay" not in c.entry.compat_config.fixes:
            cfg = restore.setdefault("compat", c.entry.compat_config.to_dict())
            cfg["fixes"] = sorted(set(cfg.get("fixes") or []) | {"no_steam_overlay"})
    one = len(diffs) == 1
    expl = (f"The game started fine on {when}, and has failed since. "
            + ("Exactly one thing changed in between, which makes it the prime suspect: " if one else
               f"{len(diffs)} things changed in between: ") + "; ".join(lines) + ". "
            + ("Going back to the setup that worked is the most direct fix (your other settings are kept)."
               if restore else "These cannot be undone from here, but they point at the cause."))
    return [Finding("regression", f"It worked on {when} — {'one thing' if one else f'{len(diffs)} things'} changed "
                    "since", expl, 0.85 if one else 0.72, ev,
                    Fix("restore_setup", _json.dumps(restore), "Go back to the setup that worked")
                    if restore else None)]


def r_exit_code(c: Context) -> list[Finding]:
    """What the exit code says (Proton passes a Windows crash status on as its lowest byte: 0xC0000135 → 53)."""
    from ezgd.library import history
    if not c.run.found or c.run.seconds is None or (c.run.seconds or 0) > 600:
        return []
    ex = history.explain_exit(c.run.exit_code)
    if ex is None:
        return []
    kind, text, certain = ex
    ev = [f"exit code {c.run.exit_code} after {c.run.seconds}s"]
    advice = {
        "dll": "The loader stopped before the game's own code ran. A diagnostic launch names the DLL; missing "
               "runtimes (Visual C++, DirectX), a fix that is not in the game's folder, or a DLL override pointing "
               "at a file that is not there are the usual reasons.",
        "crash": "The game crashed. The other findings say where; switching Proton or the renderer often helps.",
        "cpu": "Some games require AVX/AVX2 or newer instructions; under Wine this also happens with "
               "anti-tamper code. A different Proton (GE-Proton) is worth a try.",
        "bitness": "A 64-bit game loaded a 32-bit DLL (or the other way round) — typically a DLL copied into the "
                   "game folder or a wrong override. Turning off the game's own DLLs is the quickest test.",
        "dllinit": "A DLL's start-up code failed — often .NET, Visual C++ runtimes, or the Proton version.",
        "dotnet": "The game is a .NET program and threw an error at start — usually a missing .NET runtime or "
                  "fonts; the .NET finding (if any) has the fix.",
        "failfast": "Frequently the renderer (try DirectX 11 / WineD3D) or a crash reporter.",
        "memory": "Close other programs, or try the game with fewer graphics settings.",
        "killed": "Check that the computer has enough free memory (and swap) for this game.",
        "heap": "Proton's heap workaround (delay freeing memory) fixes many of these.",
    }.get(kind, "")
    cpu_note = ""
    if kind == "cpu":
        try:
            flags = open("/proc/cpuinfo", encoding="utf-8", errors="replace").read(200000)
            missing = [f for f in ("avx", "avx2") if not re.search(rf"\b{f}\b", flags)]
            if missing:
                cpu_note = f" This processor lacks {', '.join(missing).upper()}, which modern games require."
                ev.append("CPU without " + ", ".join(missing).upper())
        except OSError:
            pass
    return [Finding(f"exit:{kind}", ("Exit code: " if certain else "Exit code probably means: ") + text.split(":")[0],
                    f"{c.run.exit_code} is {'' if certain else 'most likely '}{text}. {advice}{cpu_note}",
                    (0.5 if certain else 0.35) + (0.3 if cpu_note else 0), ev,
                    fixable=kind not in ("cpu",) or not cpu_note)]


def r_override_files(c: Context) -> list[Finding]:
    """A DLL forced to "native only" (name=n) that does not exist: Windows' loader then fails (exit code 53)."""
    m = re.search(r"WINEDLLOVERRIDES=(?:'([^']*)'|\"([^\"]*)\"|(\S+))", c.run.text[:20000])
    if not m:
        return []
    spec = next(g for g in m.groups() if g is not None)
    pfx = c.prefix_dir / "pfx" / "drive_c" / "windows"
    root = Path(c.entry.install_path or c.exe.parent)
    where = [c.exe.parent, root, pfx / "system32", pfx / "syswow64"]
    missing = []
    for part in spec.split(";"):
        if "=" not in part:
            continue
        names, mode = part.split("=", 1)
        if mode.strip() != "n":
            continue
        for name in names.split(","):
            n = name.strip()
            if n and not any((d / f"{n}.dll").exists() for d in where):
                missing.append(n)
    if not missing:
        return []
    own = [n for n in missing if re.search(rf"(^|[;,]){re.escape(n)}(,[^=;]*)?=n($|;)",
                                           c.entry.compat_config.dll_overrides or "")]
    fix = None
    if own:
        fixed = re.sub(r"=n(;|$)", r"=n,b\1", c.entry.compat_config.dll_overrides)
        fix = Fix("set_overrides", fixed, "Let Wine use its own copy when the file is missing (n,b)")
    return [Finding("override-missing", f"A DLL forced to load from the game folder is not there: "
                    f"{', '.join(n + '.dll' for n in missing[:4])}",
                    "The launch tells Wine to load these DLLs only from the game's own files (=n), but the files "
                    "do not exist, so Windows' loader stops the game before it starts (often exit code 53). "
                    + ("Changing the override to n,b lets Wine fall back to its own copy." if own else
                       "They come from a switched-on fix or OnlineFix's override list — the fix's DLLs are probably "
                       "missing from the game folder."), 0.7, [f"WINEDLLOVERRIDES={spec[:200]}"], fix)]


def _failing(c: Context) -> bool:
    return c.run.found and c.run.exit_code not in (None, 0, -15, 143) and (c.run.seconds or 99) < 60


def r_steam_reference(c: Context) -> list[Finding]:
    """The same game as Steam runs it: what Steam's prefix has that this one lacks, and which Proton Steam uses."""
    ref = c.steam_ref
    if not ref:
        return []
    out: list[Finding] = []
    win = c.prefix_dir / "pfx" / "drive_c" / "windows"
    ours64 = {p.name.lower() for p in (win / "system32").glob("*.dll")} if (win / "system32").is_dir() else set()
    ours32 = {p.name.lower() for p in (win / "syswow64").glob("*.dll")} if (win / "syswow64").is_dir() else set()
    if not ours64:                             # no prefix yet: what our Proton provides
        ours64 = set(proton_dlls(c.proton_dir, 64))
        ours32 = set(proton_dlls(c.proton_dir, 32))
    have = installed_verbs(c.prefix_dir)
    verbs: dict[str, tuple[str, list[str]]] = {}
    for theirs, ours in ((ref.get("dlls64") or [], ours64), (ref.get("dlls32") or [], ours32)):
        for dll in set(theirs) - ours:
            rt = _runtime_for(dll)
            if rt and rt[0] and rt[0] not in have:
                if dll not in verbs.setdefault(rt[0], (rt[1], []))[1]:
                    verbs[rt[0]][1].append(dll)
    failing = _failing(c)
    if verbs:
        names = "; ".join(f"{what} ({', '.join(sorted(dlls)[:3])})" for what, dlls in verbs.values())
        out.append(Finding("steam-ref-runtime", "Steam's own setup for this game has runtimes this one lacks",
                           f"Steam has run this game itself (prefix compatdata/{ref['app_id']}), and on its first start "
                           f"installed: {names}. The app's prefix for the game does not have them, so the game can "
                           "miss a DLL it needs (Windows' “DLL not found”, exit code 53). Installing the same "
                           "components makes the two setups match.", 0.8 if failing else 0.5,
                           [f"in Steam's prefix, not in this one: {', '.join(sorted({d for _w, ds in verbs.values() for d in ds})[:8])}"],
                           Fix("winetricks", " ".join(sorted(verbs)), "Install what Steam installed for it")))
    sp = ref.get("proton")
    if sp and sp != c.proton_name:
        out.append(Finding("steam-ref-proton", f"Steam runs this game with {sp}",
                           f"The last time Steam started this game it used {sp} (prefix version {ref.get('version')}); "
                           f"the app uses {c.proton_name or 'another Proton'}. If the game works when Steam starts "
                           f"it, using {sp} here too is the closest match.", 0.6 if failing else 0.3,
                           [f"compatdata/{ref['app_id']}/config_info → {sp}"],
                           Fix("proton", sp, f"Use {sp}, like Steam")))
    return out


def r_redist(c: Context) -> list[Finding]:
    """Redistributable installers the release ships (Steam or the release's instructions would run them first)."""
    from ezgd.library import steamref
    root = Path(c.entry.install_path or c.exe.parent)
    have = installed_verbs(c.prefix_dir)
    found = [(v, w, f) for v, w, f in steamref.shipped_redists(root) if not set(v.split()) <= have]
    if not found:
        return []
    failing = _failing(c)
    return [Finding("redist", "The game ships runtimes that are not installed yet",
                    "The game's folder contains installers for: " + "; ".join(w for _v, w, _f in found)
                    + ". Windows installs these before the first start (Steam does it automatically); in this "
                    "game's prefix they are missing. The app installs the same components with winetricks instead of "
                    "running the bundled installers.", 0.75 if failing else 0.45,
                    [Path(f).name for _v, _w, f in found][:5],
                    Fix("winetricks", " ".join(sorted({x for v, _w, _f in found for x in v.split()})),
                        "Install the game's runtimes"))]


_UE_CAUSES = [
    (r"(device (was )?removed|DXGI_ERROR_DEVICE_(REMOVED|HUNG|RESET)|GPU crashed|D3D Device (Lost|Removed)|"
     r"VK_ERROR_DEVICE_LOST)", "gpu-lost", "The graphics driver lost the game's graphics device",
     "The game's renderer crashed on the GPU (DirectX device lost). Starting it with DirectX 11 (-dx11), another "
     "Proton, or lower graphics settings usually helps.", "-dx11"),
    (r"(out of video memory|OutOfVideoMemory|Ran out of memory|VK_ERROR_OUT_OF_DEVICE_MEMORY)", "vram",
     "The game ran out of video memory", "Lower the texture quality / resolution, or close other programs that use "
     "the GPU.", None),
    (r"(PSO|pipeline state|shader compil|ShaderCodeLibrary|Failed to create shader)", "shaders",
     "The game failed while building shaders", "A damaged shader cache or a renderer problem: rebuilding the shader "
     "caches or DirectX 11 (-dx11) fixes most cases.", "-dx11"),
    (r"(Corrupt (data|pak)|Pak.*(corrupt|signature|mismatch)|Couldn't find file|Failed to (load|open) package)",
     "files", "The game's data files are damaged or incomplete", "Repair or re-extract the game's files.", None),
    (r"(SteamAPI|Steamworks|Steam must be running|OnlineSubsystemSteam)", "steam",
     "The game's Steam connection failed", "Start Steam (signed in) before the game; for releases with a fix or "
     "emulator, check that it is in place.", None),
]


def r_engine_crash(c: Context) -> list[Finding]:
    """Unreal Engine's crash report (Saved/Crashes/*/CrashContext.runtime-xml) and its log's fatal error line."""
    if c.engine != "Unreal Engine":
        return []
    root = Path(c.entry.install_path or c.exe.parent)
    users = c.prefix_dir / "pfx" / "drive_c" / "users" / "steamuser"
    files = glob.glob(str(root / "**" / "Saved" / "Crashes" / "*" / "CrashContext.runtime-xml"), recursive=True)[:40]
    files += glob.glob(str(users / "AppData" / "Local" / "*" / "Saved" / "Crashes" / "*" / "CrashContext.runtime-xml"))
    newest = _newest(files)
    message = ""
    if newest is not None and (not c.run_started or newest.stat().st_mtime >= c.run_started - 60):
        xml = _tail(newest, 256 * 1024)
        m = re.search(r"<ErrorMessage>(.*?)</ErrorMessage>", xml, re.S)
        message = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    m = re.search(r"(Fatal error: .{0,300}|LowLevelFatalError.{0,300}|Assertion failed: .{0,300})", c.engine_log)
    fatal = m.group(1).strip() if m else ""
    text = (message + " " + fatal).strip()
    if not text:
        return []
    ev = [t for t in (message[:300], fatal[:300]) if t]
    for rx, key, title, advice, args in _UE_CAUSES:
        if re.search(rx, text, re.I):
            fix = None
            if args and args not in (c.entry.launch_args or ""):
                fix = Fix("add_args", args, "Start with DirectX 11")
            return [Finding(f"ue-crash:{key}", title, "Unreal Engine's own crash report says: “"
                            + (message or fatal)[:240] + "”. " + advice, 0.8, ev, fix)]
    return [Finding("ue-crash", "Unreal Engine reported a fatal error",
                    "The engine's own crash report says: “" + (message or fatal)[:300] + "”. That message is the best "
                    "lead — the other findings may explain it.", 0.55, ev)]


def r_mixed_prefix(c: Context) -> list[Finding]:
    """Proton of one kind (GE, CachyOS, Valve) failed to upgrade a prefix another kind made — and ran the game in it."""
    from ezgd.platform import prefixguard as pg
    ev = _lines(c.wine, r"(Upgrading prefix from \S+ to \S+|Prefix has an invalid version)", 4)
    if not any("invalid version" in x for x in ev):
        return []
    m = re.search(r"Upgrading prefix from (\S+) to (\S+)", "\n".join(ev))
    old, new = (m.group(1), m.group(2)) if m else ("another Proton", c.proton_name or "this Proton")
    if m and pg.family(old) == pg.family(new):
        return []
    return [Finding("mixed-prefix", "The game ran in a prefix another kind of Proton made",
                    f"The prefix was set up by {old}; {new} cannot upgrade prefixes of other Proton builds (its "
                    "upgrade fails with “invalid version”) and then runs the game in it anyway — which breaks games "
                    "in odd ways (OnlineFix: “Failed to load original steamclient”, crashes at start). The app now "
                    "gives every kind of Proton its own prefix for a game and copies your saves over (it also does "
                    "this by itself on the next Play).", 0.9, ev,
                    Fix("separate_prefix", "", "Give it its own prefix (saves are kept)"))]


def r_heap(c: Context) -> list[Finding]:
    """The game damages its heap (use after free / double free): Proton's PROTON_HEAP_DELAY_FREE helps."""
    ev = _lines(c.wine, r"(err:heap|heap corruption|c0000374|RtlpLogHeapFailure|HEAP: )", 3)
    if not ev or "heap_delay_free" in c.entry.compat_config.options:
        return []
    return [Finding("heap", "The game corrupts its own memory (heap error)",
                    "Wine reports heap errors: the game uses memory after freeing it, or frees it twice. Windows "
                    "happens to tolerate this; Proton's heap workaround (delay freeing memory) usually makes the game "
                    "run.", 0.7, ev, Fix("enable_fix", "opt:heap_delay_free", "Turn on Proton's heap workaround"))]


def r_filesystem(c: Context) -> list[Finding]:
    """Games on NTFS / exFAT / FAT drives: Wine needs Linux file permissions and case behaviour."""
    import subprocess
    root = Path(c.entry.install_path or c.exe.parent)
    try:
        fs = subprocess.run(["findmnt", "-no", "FSTYPE", "-T", str(root)], capture_output=True, text=True,
                            timeout=5).stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return []
    if fs not in ("ntfs", "ntfs3", "fuseblk", "exfat", "vfat", "msdos"):
        return []
    name = {"ntfs3": "NTFS", "ntfs": "NTFS", "fuseblk": "NTFS (ntfs-3g)", "exfat": "exFAT"}.get(fs, "FAT")
    return [Finding("filesystem", f"The game is on a Windows-formatted drive ({name})",
                    f"{root} is on a {name} drive. Proton works best on Linux drives (ext4, btrfs …): on {name}, "
                    "file permissions, letter case and symbolic links behave differently, which breaks some games "
                    "and their saves. If the game misbehaves, move it to a Linux drive (Library → the game → Change "
                    "install folder).", 0.55 if _failing(c) else 0.3, [f"findmnt: {fs}"], fixable=False)]


def r_onlinefix_missing(c: Context) -> list[Finding]:
    """An OnlineFix game without OnlineFix's files: Play starts the bare game, which quits (Steam API init fails)."""
    import json as _json
    if c.entry.source != "onlinefix":
        return []
    root = Path(c.entry.install_path or c.exe.parent)
    folders = {c.exe.parent, root, *list(c.exe.parents)[:4]}
    if any((f / n).exists() for f in folders for n in ("OnlineFix64.dll", "OnlineFix.dll", "OnlineFix.ini")):
        return []
    try:
        if any(True for n in ("OnlineFix64.dll", "OnlineFix.ini") for _ in root.rglob(n)):
            return []
    except OSError:
        pass
    from ezgd.installation import archives as ar
    archive = None
    if c.entry.download_path:
        dp = Path(c.entry.download_path)
        if dp.is_file() and ar.detect_format(dp) is not None:
            archive = dp
        elif dp.is_dir():
            archive = next(iter(ar.find_archives(dp, 2)), None)
    ev = [f"no OnlineFix64.dll / OnlineFix.ini in {root}"]
    quick = c.run.found and (c.run.seconds or 99) < 15
    if quick:
        ev.append(f"the last start ended after {c.run.seconds}s (exit code {c.run.exit_code})")
    return [Finding("onlinefix-missing", "OnlineFix's fix is not in the game's folder",
                    "This is an OnlineFix game, but its fix (OnlineFix64.dll and its settings) is not in the folder "
                    "Play starts from — so the game starts without it and quits or cannot go online. "
                    + (f"Applying {archive.name} puts it there (every file it replaces is backed up)."
                       if archive else "Download the game's fix again from its page."),
                    0.92 if quick else 0.8, ev,
                    Fix("apply_archive", _json.dumps({"archive": str(archive), "dest": str(root)}),
                        f"Apply {archive.name}") if archive else None)]


def r_overlay(c: Context) -> list[Finding]:
    """The Steam overlay's library crashed the game (it is loaded into every game the app starts with the overlay)."""
    # (“wrong ELF class … ignored” is normal: Steam preloads its 32- and 64-bit overlay into every program)
    ev = [ln for ln in _lines(c.wine, r"(gameoverlayrenderer\S*\.so.*(segfault|SIGSEGV|crash)|"
                                      r"(segfault|SIGSEGV|crash|backtrace).*gameoverlayrenderer)")
          if "wrong ELF class" not in ln]
    if not ev or "no_steam_overlay" in c.entry.compat_config.fixes:
        return []
    return [Finding("overlay-crash", "The Steam overlay crashes the game",
                    "The log shows Steam's overlay library failing inside the game. Turning the overlay off for "
                    "this game only usually fixes it (Shift+Tab will not open Steam in it).", 0.75, ev,
                    Fix("enable_fix", "no_steam_overlay", "Turn the Steam overlay off for this game"))]


def r_fonts(c: Context) -> list[Finding]:
    ev = _lines(c.everything, r"(font.*(not found|missing|could not (be )?(loaded|found))|"
                              r"(Arial|Tahoma|Times New Roman|Verdana|Segoe UI).*not found|"
                              r"err:font|Failed to (load|create) font)")
    if not ev or "corefonts" in installed_verbs(c.prefix_dir):
        return []
    return [Finding("fonts", "The game cannot find Windows fonts",
                    "Some games crash or show no text without Microsoft's core fonts (Arial, Times New Roman …), "
                    "which Wine does not include.", 0.55, ev,
                    Fix("winetricks", "corefonts", "Install Windows core fonts"))]


def r_display(c: Context) -> list[Finding]:
    ev = _lines(c.wine, r"(cannot open display|Can't open display|no driver could be loaded|"
                        r"nodrv_CreateWindow|Failed to (connect to|open) (the )?(X|wayland) (server|display)|"
                        r"Application tried to create a window, but no driver could be loaded)")
    if not ev:
        return []
    wayland = "wayland" in c.entry.compat_config.options
    return [Finding("display", "The game cannot open a window",
                    "Wine could not connect to the desktop's display. "
                    + ("The Wayland option is on for this game; turning it off makes Wine use XWayland, which "
                       "works with every Proton." if wayland else
                       "This usually happens when XWayland is not running or the game is started outside the "
                       "desktop session. Log out and in again, or try another Proton."), 0.7, ev)]


def r_disk(c: Context) -> list[Finding]:
    import shutil
    out = []
    for where, path in (("game folder", Path(c.entry.install_path or c.exe.parent)), ("prefix", c.prefix_dir)):
        try:
            free = shutil.disk_usage(path if path.exists() else path.parent).free
        except OSError:
            continue
        if free < 1024 ** 3:
            out.append(Finding("disk-space", f"Almost no free disk space ({free // (1024 ** 2)} MB)",
                               f"The drive with the {where} has less than 1 GB free. Games crash or fail to save "
                               "without room for shader caches, logs and saves. Free up some space and try again.",
                               0.65 if free < 300 * 1024 ** 2 else 0.4, [f"{path}: {free // (1024 ** 2)} MB free"],
                               fixable=False))
            break
    if re.search(r"(No space left on device|ENOSPC|disk (is )?full)", c.wine, re.I):
        out = [Finding("disk-space", "The drive ran full", "The log says the drive has no space left. Free up some "
                       "space and start the game again.", 0.85,
                       _lines(c.wine, r"(No space left on device|ENOSPC|disk (is )?full)"), fixable=False)]
    return out


def r_writable(c: Context) -> list[Finding]:
    root = Path(c.entry.install_path or c.exe.parent)
    if not root.is_dir() or os.access(root, os.W_OK):
        ev = _lines(c.wine, r"(Permission denied|EACCES|Access is denied|read-only file system)")
        if not ev:
            return []
        return [Finding("permissions", "The game is not allowed to write a file",
                        "The log shows the game being refused access to a file. Games installed in a folder you do "
                        "not own, or on an NTFS drive mounted read-only, cannot save settings or saves. Move the "
                        "game to a folder in your home folder, or check the drive's permissions.", 0.4, ev,
                        fixable=False)]
    return [Finding("permissions", "The game's folder is read-only",
                    f"{root} cannot be written to, so the game cannot save its settings or progress (many games "
                    "crash because of that). Move the game into a folder in your home folder or fix the folder's "
                    "permissions.", 0.6, [f"{root}: not writable"], fixable=False)]


_NOISE = re.compile(r"(ProtonFixes\[\d+\] (INFO|WARN)|^Proton: |fixme:|pressure-vessel|^PRESSURE_VESSEL|"
                    r"wrong ELF class|^ntsync|skipping destruction|^Fossilize|trace:loaddll|warn:module:|"
                    r"^\s*$|^=== |^#ezgd-setup|steam-runtime|^STEAM_COMPAT|umu\.exe|^[0-9a-f]+:trace:seh)", re.I)


def _last_words(text: str, n: int = 3) -> list[str]:
    """The last meaningful lines of a run (what the game or Wine said just before it ended)."""
    out = []
    for ln in reversed(text.splitlines()[-400:]):
        ln = ln.strip()
        if not ln or _NOISE.search(ln):
            continue
        ln = re.sub(r"^[0-9a-f]{4,8}:", "", ln)[:220]
        if ln not in out:
            out.append(ln)
        if len(out) >= n:
            break
    return list(reversed(out))


_SUPPORTS = {          # exit-code meaning → finding keys that are consistent with it
    "dll": ("missing-dll", "imports", "runtime", "onlinefix-missing", "override-missing", "steam-ref", "redist", "dotnet", "fix-archive", "emulator",
            "steam", "advisor", "native-lib"),
    "dllinit": ("dotnet", "runtime", "missing-dll", "imports", "advisor"),
    "dotnet": ("dotnet", "fonts"),
    "crash": ("graphics", "crash", "dump", "dx12", "vulkan", "overlay-crash", "unimplemented", "media",
              "ue-crash"),
    "failfast": ("graphics", "crash", "dump", "dx12", "vulkan", "overlay-crash"),
    "bitness": ("missing-dll", "imports", "advisor", "toggle"),
    "memory": ("memory", "disk-space"),
    "killed": ("memory",),
    "heap": ("heap",),
}


def _reason(c: Context, findings: list[Finding]) -> list[dict]:
    """The assistant's reasoning, step by step: what happened, how far the game got, what the evidence points to,
    what agrees with it, what was ruled out, and what to do next. Also raises findings that agree with each other."""
    from ezgd.library import history
    out: list[dict] = []

    def say(kind: str, text: str) -> None:
        out.append({"kind": kind, "text": text})
    run = c.run
    if not run.found:
        return out
    ex = history.explain_exit(run.exit_code)
    if run.exit_code is None:
        say("what", "The last start has no recorded end — it may still be running, or the app was closed.")
    else:
        what = f"The last start ran {run.seconds}s and ended with exit code {run.exit_code}"
        if ex:
            what += f" — {'that is' if ex[2] else 'most likely'} {ex[1]}"
        elif run.exit_code == 0 and (run.seconds or 0) < 15:
            what += " — a clean exit after a few seconds: either you closed it, or a launcher/stub quit by itself"
        elif run.exit_code == 0:
            what += " — a normal exit"
        say("what", what + ".")
    if c.investigated:
        say("what", "This analyses the assistant's own test run (current setup, a throw-away copy of the prefix, "
                    "with Wine's detailed logging) — not your last start.")
    last = _last_words(run.text)
    if last and (run.exit_code not in (None, 0) or (run.seconds or 0) < 60):
        say("last", "The last things it logged before it ended: " + " | ".join(last))
    ph = history.phase(run.text + "\n" + c.engine_log)
    if ph.reached:
        nxt = {"Proton started": "before the game set up Steam or graphics",
               "Steam API connected": "after connecting to Steam, before graphics started",
               "graphics started": "while starting graphics, before a window was shown",
               "game window opened": "after its window opened, before the game finished loading",
               "game loaded": "after the game had loaded"}.get(ph.last, "")
        say("phase", f"How far it got: {' → '.join(ph.reached)}. So it stopped {nxt}.")
    bad = _latest(c)
    good = history.last_worked(c.history, bad) if bad is not None else None
    if "game loaded" in ph.reached and run.exit_code is not None and (run.seconds or 0) < 300:
        say("what", "Its logs show it reached the game (e.g. the main menu) before it ended — so it does start. "
                    "If you closed it yourself, nothing is wrong; otherwise choose what happens under “What goes "
                    "wrong?” so the assistant looks for a problem in the game rather than at start-up.")
    if good is not None and bad is not None and bad.failed:
        say("history", f"It worked on {good.started[:16]} ({good.seconds // 60} min) and has failed since — "
                       "comparing the two setups is the strongest lead.")
    elif c.history and not any(r.worked for r in c.history):
        say("history", f"None of its {len(c.history)} start(s) from the app got into the game so far.")
    # a regression is a lead; direct, specific evidence of a cause (a named DLL, a crash site) stays first
    direct = max((f.confidence for f in findings if not f.key.startswith(("regression", "exit:", "rule-error"))),
                 default=0.0)
    for f in findings:
        if f.key == "regression" and direct >= 0.6 and f.confidence >= direct:
            f.confidence = max(0.5, direct - 0.05)
    # agreement between the exit code and the findings raises those findings
    if ex:
        keys = _SUPPORTS.get(ex[0], ())
        agree = [f for f in findings if any(k in f.key for k in keys) and f.confidence > 0]
        for f in agree:
            f.confidence = min(0.97, f.confidence + 0.08)
        if agree:
            say("agree", "The exit code agrees with: " + "; ".join(f.title for f in agree[:3]) + ".")
    ranked = sorted([f for f in findings if f.confidence > 0 and not f.key.startswith(("exit:", "rule-error"))],
                    key=lambda f: -f.confidence)
    if ranked:
        top = ranked[0]
        say("cause", f"Most likely cause ({round(top.confidence * 100)}% sure): {top.title}."
                     + (f" Evidence: {top.evidence[0]}" if top.evidence else ""))
        if len(ranked) > 1 and ranked[1].confidence >= top.confidence - 0.1:
            say("cause", f"Almost as likely: {ranked[1].title} ({round(ranked[1].confidence * 100)}%).")
    # what was checked and is fine — so the user (and the assistant) can stop suspecting it
    ok = []
    keys = " ".join(f.key for f in findings)
    text = c.everything
    if not re.search(r"(missing-dll|imports|runtime)", keys) and not re.search(r"not found", c.wine, re.I):
        ok.append("no missing DLL is named in the log" + ("" if run.diagnostic else " (a diagnostic launch checks "
                                                                                     "this thoroughly)"))
    if c.vulkan64 and re.search(r"(DXVK: v\d|vkd3d-proton|Vulkan API version)", text):
        ok.append("the graphics driver works (DXVK/VKD3D started)")
    elif c.vulkan64:
        ok.append("a Vulkan driver is installed")
    if "anticheat" not in keys:
        root = Path(c.entry.install_path or c.exe.parent)
        if not any((root / n).exists() for n in ("EasyAntiCheat", "BattlEye", "EasyAntiCheat_EOS_Setup.exe")):
            ok.append("no anti-cheat in the game")
    if c.integrity and not (c.integrity.get("missing") or c.integrity.get("damaged")) and c.integrity.get("archives"):
        ok.append("the game's files match the download")
    if "disk-space" not in keys:
        ok.append("there is enough free disk space")
    if ok:
        say("ruled_out", "Checked and fine: " + "; ".join(ok) + ".")
    fixable = next((f for f in ranked if f.fix), None)
    if fixable is not None:
        say("next", f"Next step: “{fixable.fix.label}”. If that does not help, “Try fixes for me” tests the "
                    "other candidates automatically.")
    elif not run.diagnostic:
        say("next", "Next step: a diagnostic launch — it records which DLLs load, every exception and where it "
                    "crashed, which usually turns a guess into a clear answer.")
    else:
        say("next", "Next step: “Try everything” — the evidence does not point at one fix, so testing many setups "
                    "is the fastest way to find one that works.")
    return out


RULES = [r_integrity, r_emulator, r_fix_archive, r_missing_dll, r_unimplemented, r_imports, r_dotnet, r_steam, r_anticheat, r_vulkan, r_dx12, r_media,
         r_crash, r_minidump, r_prefix, r_quick_exit, r_native, r_system, r_protondb, r_old_prefix, r_advisor,
         r_overlay, r_fonts, r_display, r_disk, r_writable, r_onlinefix_missing, r_regression, r_exit_code, r_override_files,
         r_steam_reference, r_redist, r_engine_crash, r_mixed_prefix, r_heap, r_filesystem]


def diagnose(entry: LibraryEntry, prefix_dir: Path, log_path: Path, *, protons: list, proton_name: str,
             game_extra: dict | None = None, system=None, protondb: dict | None = None, symptom: str = "",
             learned: list[dict] | None = None, integrity: dict | None = None, methods: list | None = None,
             install_dest: Path | None = None, community: dict | None = None, steam_ref: dict | None = None,
             evidence: dict | None = None) -> Diagnosis:
    """``evidence``: the assistant's own diagnostic test run ({"text", "exit_code", "seconds", "label"}), analysed
    instead of the last run in the game's log."""
    exe = Path(entry.executable) if entry.executable else None
    if exe is None or not exe.exists():
        return Diagnosis("No executable is selected (or it was deleted), so the game cannot start.",
                         [Finding("no-exe", "Choose the game's executable",
                                  "Open the game in the Library and pick a launch file under “Launch files”.", 0.95)],
                         [], {}, False)
    run = last_run(log_path)
    if evidence:
        run = LastRun(started=time.strftime("%Y-%m-%d %H:%M:%S"),
                      description=f"investigation: {evidence.get('label', 'current setup')} · diagnostic run",
                      diagnostic=True, exit_code=evidence.get("exit_code"), seconds=evidence.get("seconds"),
                      text=evidence.get("text") or "")
    engine = engine_of(exe)
    log_name, engine_log = engine_logs(exe, prefix_dir, engine)
    if log_name and run.started:                 # a log from an older run would mislead
        try:
            started = time.mktime(time.strptime(run.started, "%Y-%m-%d %H:%M:%S"))
            if Path(log_name).stat().st_mtime < started - 60:
                log_name, engine_log = "", ""
        except (OSError, ValueError):
            pass
    v64, v32 = vulkan_icds()
    proton = next((p for p in protons if p.name == proton_name), protons[0] if protons else None)
    started = 0.0
    if run.started:
        try:
            started = time.mktime(time.strptime(run.started, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            started = 0.0
    from ezgd.library import history
    hist = history.runs(log_path)
    if hist and hist[-1].started == run.started and "game loaded" in history.phase(engine_log).reached:
        hist[-1].reached_game = True             # the engine log of this run shows the game had loaded
    c = Context(entry, exe, prefix_dir, run, engine, log_name, engine_log, pe_is_32bit(exe), protons, proton_name,
                v64, v32, game_extra or {}, system=system, proton_dir=getattr(proton, "path", None),
                protondb=protondb, run_started=started, integrity=integrity, methods=list(methods or []),
                install_dest=install_dest, community=community, history=hist, steam_ref=steam_ref,
                investigated=bool(evidence))
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings += rule(c)
        except Exception as exc:                 # one broken rule must not hide the others
            findings.append(Finding(f"rule-error:{rule.__name__}", "Part of the check failed", str(exc), 0.0))
    # components already installed into this prefix are not offered again ("Install" kept coming back)
    have = installed_verbs(prefix_dir)
    for f in findings:
        if f.fix and f.fix.kind == "winetricks" and f.fix.value and set(f.fix.value.split()) <= have:
            f.explanation += (f" — {f.fix.value} is already installed in this game's prefix. If the game still "
                              "fails, the cause is elsewhere (see the other findings or Try fixes for me).")
            f.title += " (already installed)"
            f.fix = None
            f.confidence = min(f.confidence, 0.3)
    try:
        reasoning = _reason(c, findings)          # (also raises findings the exit code agrees with)
    except Exception as exc:
        reasoning = [{"kind": "what", "text": f"(The reasoning step failed: {exc})"}]
    # strongest evidence first; the same fix is offered once
    findings = [f for f in findings if f.confidence > 0]
    findings.sort(key=lambda f: (-f.confidence, not f.fix))
    seen_fix, uniq = set(), []
    for f in findings:
        k = (f.fix.kind, f.fix.value) if f.fix else None
        if k and k in seen_fix:
            f.fix = None
        if k:
            seen_fix.add(k)
        uniq.append(f)
    facts = []
    if engine:
        facts.append(f"Engine: {engine}")
    if c.is32 is not None and is_windows_binary(exe):
        facts.append(f"{'32' if c.is32 else '64'}-bit Windows game")
    facts.append(f"Proton: {proton_name or 'newest detected'}")
    if system is not None and getattr(system, "gpus", None):
        facts.append("GPU: " + ", ".join(f"{g.name} ({g.driver})" for g in system.gpus))
    if protondb and protondb.get("tier"):
        facts.append(f"ProtonDB: {protondb['tier']} ({protondb.get('total', '?')} reports)")
    if community and community.get("r"):
        facts.append(f"ProtonDB players: {community['ok']} of {community['r']} report it working")
    if log_name:
        facts.append(f"Game log read: {log_name}")
    if not v64:
        facts.append("No Vulkan driver found on this system")
    if run.found:
        how = (f"exited with code {run.exit_code} after {run.seconds}s" if run.exit_code is not None
               else "has no recorded exit (still running, or the app was closed)")
        summary = f"Last run {run.started}: {how}."
        if run.exit_code == 0 and (run.seconds or 0) > 60 and not uniq:
            summary += " It ran for a while and exited normally — no problem is visible in the logs."
    else:
        summary = "The game has not been started from the app yet, so there is no run to analyse."
    if not uniq:
        summary += (" No known cause was found in the logs." +
                    ("" if run.diagnostic else " A diagnostic launch records much more detail."))
    else:
        top = uniq[0]
        if top.confidence >= 0.5:
            summary += f" Most likely cause: {top.title}."
        elif not run.found:
            summary += (" Press Play once (or “Investigate automatically”) so the assistant has a run to look at; "
                        f"so far there is only a weak lead: {top.title}.")
        else:
            summary += f" No clear cause yet — the strongest lead is weak: {top.title}."
    from ezgd.library import fixgen
    gen = []
    if community:                                  # what players did matters for "Proton version" advice too
        top = [p for p, _n in community.get("protons", [])]
        if top and any(p.startswith("GE-Proton") for p in top[:2]) and not any("ge" in getattr(p, "name", "").lower()
                                                                              for p in protons):
            facts.append("Most players who fixed it used GE-Proton (" + ", ".join(top[:2]) + ") — install it from "
                         "Settings → Library")
    try:
        for g in fixgen.generate(c, [f.to_dict() for f in uniq], symptom, learned):
            d = g.to_dict()
            d["overrides"] = g.overrides(entry)
            gen.append(d)
    except Exception as exc:                     # the generator must not break the diagnosis
        facts.append(f"Fix generator failed: {exc}")
    deep = []
    try:
        have = {g["gen_id"] for g in gen}
        for g in fixgen.generate(c, [f.to_dict() for f in uniq], symptom, learned, deep=True):
            if g.gen_id not in have:
                have.add(g.gen_id)
                d = g.to_dict()
                d["overrides"] = g.overrides(entry)
                deep.append(d)
    except Exception as exc:
        facts.append(f"Fix generator (try everything) failed: {exc}")
    if symptom and symptom in fixgen.SYMPTOMS:
        summary += f" You reported: {fixgen.SYMPTOMS[symptom].lower()}."
        if gen:
            summary += f" The assistant built {len(gen)} fix(es) for that — test them with “Try fixes for me”."
    return Diagnosis(summary, uniq, facts,
                     {"started": run.started, "exit_code": run.exit_code, "seconds": run.seconds,
                      "diagnostic": run.diagnostic, "description": run.description},
                     suggest_diagnostic_run=not run.diagnostic and (not uniq or uniq[0].confidence < 0.7),
                     generated=gen, symptom=symptom, generated_deep=deep, reasoning=reasoning)


__all__ = ["diagnose", "Diagnosis", "Finding", "Fix", "last_run"]
