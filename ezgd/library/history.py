"""Every launch of a game, read back from its log — so the debugging assistant can reason over time.

Each launch writes a header (``=== <time> <description>``), since 2026-10-12 a ``#ezgd-setup {json}`` line with the
exact setup (Proton build, fixes, options, launch options, executable, kernel), and ``=== exited with code N after
Ss`` when it ends. From that the assistant can tell that a game *used to work* and what changed since, and decode
what the exit code means.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

READ_LIMIT = 8 * 1024 * 1024
SETUP_MARK = "#ezgd-setup "

_HEADER = re.compile(r"^=== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
_EXIT = re.compile(r"^=== exited with code (-?\d+) after (\d+)s$")
_SKIP = ("winetricks", "exited", "installer", "trial ", "setup ")

WORKED_SECONDS = 120          # a run this long got into the game
FAILED_SECONDS = 60


@dataclass
class Run:
    started: str
    description: str
    setup: dict | None = None
    exit_code: int | None = None
    seconds: int | None = None
    text: str = ""
    at: float = 0.0

    @property
    def diagnostic(self) -> bool:
        return "diagnostic run" in self.description

    @property
    def worked(self) -> bool:
        """It got into the game: ran for minutes (a crash after that is a different problem), or was stopped."""
        if self.seconds is None:
            return False
        return self.seconds >= WORKED_SECONDS or (self.exit_code in (-15, 143) and self.seconds >= 30)

    reached_game: bool = False        # its logs show the game loaded (set by the assistant from the engine log)

    @property
    def failed(self) -> bool:
        """It did not get going: quit within a minute with an error, or within seconds at all."""
        if self.seconds is None or self.exit_code is None or self.exit_code in (-15, 143):
            return False
        if self.reached_game or "game loaded" in phase(self.text).reached:
            return False                 # it got into the game: closed by the player, or a later problem
        return self.seconds < FAILED_SECONDS and (self.exit_code != 0 or self.seconds < 15)

    def facet(self, key: str):
        """One aspect of the setup, from the snapshot or (older runs) the header text."""
        s = self.setup or {}
        if key in s:
            return s[key]
        d = self.description
        if key == "proton":
            first = d.split(" · ", 1)[0]
            return first if re.search(r"proton", first, re.I) else None
        if key == "through_steam":
            return "through Steam" in d
        if key == "overlay":
            if "Steam overlay (Shift+Tab)" in d or "overlay loaded" in d:
                return "full"
            return "friends list" if "friends list only" in d else "off"
        if key == "fixes":
            m = re.search(r"fixes: ([^·]+)", d)
            return sorted(x.strip() for x in m.group(1).split(",")) if m else []
        return None


def runs(log_path: Path, limit: int = 60) -> list[Run]:
    """Game launches in the log, oldest first (winetricks, installer and trial runs are left out)."""
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as fh:
            if size > READ_LIMIT:
                fh.seek(size - READ_LIMIT)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out: list[Run] = []
    cur: Run | None = None
    body: list[str] = []

    def close() -> None:
        if cur is not None:
            cur.text = "\n".join(body[-4000:])
            out.append(cur)
    for ln in text.splitlines():
        m = _HEADER.match(ln)
        if m:
            if cur is not None and cur.exit_code is None:
                close()                                  # no exit line (the app was closed meanwhile)
            cur, body = None, []
            if m.group(2).startswith(_SKIP):
                continue
            cur = Run(m.group(1), m.group(2))
            try:
                cur.at = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
            continue
        if cur is None:
            continue
        if ln.startswith(SETUP_MARK):
            try:
                cur.setup = json.loads(ln[len(SETUP_MARK):])
            except ValueError:
                pass
            continue
        e = _EXIT.match(ln)
        if e:
            cur.exit_code, cur.seconds = int(e.group(1)), int(e.group(2))
            close()
            cur, body = None, []
            continue
        body.append(ln)
    if cur is not None:
        close()
    return out[-limit:]


FACETS = [("proton", "Proton"), ("proton_build", "Proton build"), ("executable", "executable"),
          ("launch_args", "launch options"), ("fixes", "fixes"), ("options", "compatibility options"),
          ("dll_overrides", "DLL overrides"), ("use_runtime", "Steam Linux Runtime"), ("protonfixes", "protonfixes"),
          ("through_steam", "started through Steam"), ("overlay", "Steam overlay"), ("kernel", "Linux kernel")]


def changes(good: Run, bad: Run) -> list[tuple[str, str, object, object]]:
    """(key, what, then, now) for every aspect that differs between a run that worked and one that failed.
    Aspects only one of the two runs recorded are skipped (old logs have fewer details)."""
    out = []
    for key, what in FACETS:
        a, b = good.facet(key), bad.facet(key)
        if a is None or b is None:
            continue
        if isinstance(a, list) or isinstance(b, list):
            a, b = sorted(a or []), sorted(b or [])
        if a != b:
            out.append((key, what, a, b))
    return out


def last_worked(rs: list[Run], before: Run | None = None) -> Run | None:
    pool = rs[:rs.index(before)] if before in rs else rs
    return next((r for r in reversed(pool) if r.worked), None)


# ------------------------------------------------------------------ exit codes
# Under Wine/Proton a Windows crash status reaches Linux as its lowest byte (0xC0000135 → 53). The full codes
# are listed so an exit code that already is the full status (some launchers) is understood too.
NTSTATUS = {
    0xC0000135: ("dll", "Windows' “DLL not found” (0xC0000135): a DLL the game needs is missing"),
    0xC0000005: ("crash", "an access violation (0xC0000005): the game crashed"),
    0xC000001D: ("cpu", "an illegal instruction (0xC000001D): the game used a CPU feature this processor or "
                        "Wine does not offer"),
    0xC000007B: ("bitness", "an invalid image (0xC000007B): a 32-bit and a 64-bit DLL were mixed"),
    0xC0000142: ("dllinit", "a DLL failed to initialise (0xC0000142)"),
    0xE0434352: ("dotnet", "an unhandled .NET exception (0xE0434352)"),
    0xC0000409: ("failfast", "a fail-fast / security check (0xC0000409): the game stopped itself on purpose "
                             "(often its renderer or crash handler)"),
    0xC00000FD: ("crash", "a stack overflow (0xC00000FD)"),
    0xC0000017: ("memory", "out of memory (0xC0000017)"),
    0xC06D007E: ("dll", "a delay-loaded DLL was not found (0xC06D007E)"),
    0xC0000094: ("crash", "a division by zero (0xC0000094)"),
    0xC0000374: ("heap", "heap corruption (0xC0000374): the game damaged its own memory — often memory used after "
                         "it was freed"),
}
_BY_LOW_BYTE = {code & 0xFF: (code, kind, text) for code, (kind, text) in NTSTATUS.items()}
SIGNALS = {9: ("killed", "it was killed (SIGKILL) — often the system running out of memory"),
           11: ("crash", "Wine itself crashed (SIGSEGV)"), 6: ("crash", "it aborted (SIGABRT)"),
           7: ("crash", "a bus error (SIGBUS) — a file it had open changed or the disk had an error")}


def explain_exit(code: int | None) -> tuple[str, str, bool] | None:
    """(kind, explanation, certain) for a failing exit code; ``certain`` False = probably (a low byte can also be
    the game's own exit code)."""
    if code is None or code == 0 or code in (-15, 143, 1):
        return None
    if code < 0 and -code in SIGNALS:
        return (*SIGNALS[-code], True)
    if 128 < code < 160 and code - 128 in SIGNALS:
        return (*SIGNALS[code - 128], True)
    full = code & 0xFFFFFFFF
    if full in NTSTATUS:
        return (*NTSTATUS[full], True)
    if 0 < code < 256 and code in _BY_LOW_BYTE:
        _full, kind, text = _BY_LOW_BYTE[code]
        return kind, text, False
    return None


@dataclass
class Phase:
    reached: list[str] = field(default_factory=list)      # milestones the run got past, in order
    last: str = ""


_MILESTONES = [
    ("Proton started", r"(Proton: |umu\.exe|wine: |ntsync: up)"),
    ("Steam API connected", r"(SteamAPI_Init|Steam ?API.*(OK|initiali[sz]ed|succeeded)|\[S_API\] SteamAPI_Init|"
                            r"OnlineFix.*(loaded|initiali)|steamclient.*loaded)"),
    ("graphics started", r"(DXVK: v\d|vkd3d-proton|D3D11CoreCreateDevice|Vulkan API version|"
                         r"Initialize engine version|GfxDevice: creating device|LogD3D1[12]RHI|LogVulkanRHI|"
                         r"Godot Engine v\d|OpenGL API|Vulkan \d\.\d+\.\d+ - Forward)"),
    ("game window opened", r"(Presenter: Actual swap chain properties|Creating swapchain|SwapChain|"
                           r"DxvkSwapchain|Swapchain created|wined3d_swapchain)"),
    ("game loaded", r"(LogLoad: (Took|Game class)|Loading scene|UnloadTime|LogWorld: Bringing World|"
                    r"Scene loaded|Main menu|\(level \w*Main_?Menu\w*\))"),     # (the last: UE4SS's log)
]


def phase(text: str) -> Phase:
    p = Phase()
    for name, rx in _MILESTONES:
        if re.search(rx, text, re.I):
            p.reached.append(name)
    p.last = p.reached[-1] if p.reached else ""
    return p
