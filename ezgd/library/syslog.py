"""What the system itself logged about a game's run — failures the game cannot report because they end it from
outside: the kernel killing it when memory ran out (or systemd-oomd doing so first), the graphics driver resetting
the GPU under it (NVIDIA "Xid", amdgpu ring timeouts, i915 hangs), a crash Wine could not catch (the kernel's
"segfault at …" line) and the core dump systemd kept of it.

A game killed for memory exits with SIGKILL — the same code as the app stopping it — so without this its session
looked like a normal quit and nothing was kept. The journal is read for the run's time window only and reduced to
``ezgd:sys {...}`` marker lines (process name, pid, the fault's code — no host names, no paths), which the test runs and
play sessions append to their logs; the investigator (ezgd.library.tracer), the crash memory and Diagnose read them
from there. Where the system log cannot be read (a user outside the journal's groups), one ``ezgd:sys-unreadable``
line says so — the assistant must not take "nothing found" for "nothing happened".
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MARK = "ezgd:sys "
UNREADABLE = "ezgd:sys-unreadable"

# NVIDIA's Xid codes that matter for a game (docs.nvidia.com/deploy/xid-errors): what happened, and whose fault
XID = {
    8: ("the GPU stopped processing (timeout)", "game"),
    13: ("graphics engine exception — the GPU hit an error running the game's commands", "game"),
    31: ("GPU memory page fault — the game's GPU work read memory it should not", "game"),
    32: ("invalid or corrupted command buffer", "driver"),
    43: ("the GPU stopped the game's work after an error", "game"),
    45: ("the driver cleaned up after the game's GPU work was stopped", "after"),
    48: ("uncorrectable GPU memory error", "hardware"),
    61: ("GPU micro-controller error", "driver"),
    62: ("GPU micro-controller halted", "driver"),
    69: ("graphics engine class error", "game"),
    74: ("NVLink error", "hardware"),
    79: ("the GPU has fallen off the bus (power or PCIe problem)", "hardware"),
    92: ("high single-bit memory error rate", "hardware"),
    94: ("contained GPU memory error", "hardware"),
    95: ("uncontained GPU memory error", "hardware"),
    109: ("context switch timeout", "game"),
    119: ("GSP firmware timeout", "driver"),
    120: ("GSP firmware error", "driver"),
    154: ("the GPU needs a reset to recover", "hardware"),
}

# Wine and Proton's own processes: counted only when they are the run's (by pid) — another Wine program has them too
_HELPERS = {"wineserver", "wine64-preload", "wine-preloader", "wine64", "wine", "winedevice.exe", "explorer.exe",
            "services.exe", "plugplay.exe", "steam.exe", "rpcss.exe", "svchost.exe", "pressure-vessel"}

_OOM = re.compile(r"out of memory: Killed process (\d+) \(([^)]+)\)(?:.*?anon-rss:(\d+)kB)?", re.I)
_SEGV = re.compile(r"^(?:traps: )?(.+?)\[(\d+)\]:? (segfault at [0-9a-f]+|general protection fault|"
                   r"trap (?:invalid opcode|divide error|stack segment|int3|\w+))"
                   r"(?: ip[: ]\S+)?(?: sp[: ]\S+)?(?: error[: ]?\S+)?(?: in ([^\[\s]+)\[)?", re.I)
_SIGNAL = {"invalid opcode": "SIGILL", "divide error": "SIGFPE", "int3": "SIGTRAP"}
_XID = re.compile(r"NVRM: Xid \(PCI:[^)]*\): (\d+)(?:,\s*(.*))?")
_XID_PROC = re.compile(r"pid=(\d+|'<unknown>'),\s*name=([^,]+)")
_AMD = re.compile(r"amdgpu.*?(ring \S+ timeout|page fault|GPU reset(?: begin| succeeded| failed)?|"
                  r"GPU recovery|device lost)", re.I)
_AMD_PROC = re.compile(r"process (\S+) pid (\d+)", re.I)
_I915 = re.compile(r"(?:i915|xe) .*?(GPU HANG|Engine reset|GPU reset)", re.I)
_I915_PROC = re.compile(r"(?:in (\S+) \[(\d+)\]|process (\S+)\[(\d+)\])")
_OOMD = re.compile(r"Killed (\S+) due to (memory pressure|memory used|swap used)", re.I)


@dataclass
class Event:
    kind: str                  # oom | oomd | gpu | segfault | core
    proc: str = ""             # the process (as the kernel names it: at most 15 characters)
    pid: int | None = None
    at: float = 0.0
    code: str = ""             # nvidia:13 | amdgpu:ring-timeout | i915:hang | SIGSEGV …
    detail: str = ""           # in words
    lib: str = ""              # where a segfault happened (a file name)
    mem: int = 0               # bytes the process used when the kernel killed it
    ours: bool = True          # the run's process — False: only the time matches (a GPU fault with no process named)

    def describe(self) -> str:
        if self.kind == "oom":
            used = f", using {self.mem / (1 << 30):.1f} GB" if self.mem else ""
            return f"the kernel killed {self.proc or 'it'} because the computer ran out of memory{used}"
        if self.kind == "oomd":
            return "systemd-oomd killed it because memory was running out"
        if self.kind == "gpu":
            return f"the graphics driver reported a GPU fault ({self.code}: {self.detail})"
        if self.kind == "segfault":
            return f"{self.proc} crashed ({self.detail}" + (f" in {self.lib}" if self.lib else "") + ")"
        if self.kind == "core":
            return f"{self.proc} crashed with {self.code} (systemd kept a core dump)"
        return self.detail

    def marker(self) -> str:
        return MARK + json.dumps({k: v for k, v in asdict(self).items() if v not in ("", None, 0) or k == "ours"},
                                 sort_keys=True)


def _comm(name: str) -> str:
    """How the kernel names a process: its first 15 characters (Wine sets a game process's name to its .exe)."""
    return name[:15].lower().strip()        # (cut at 15, a name can end in a space the driver's line drops)


def _ours(proc: str, pid: int | None, names: set[str], pids: set[int]) -> bool:
    if pid is not None and pid in pids:
        return True
    c = _comm(proc)
    return bool(c) and c not in {_comm(h) for h in _HELPERS} and c in {_comm(n) for n in names}


def _message(entry: dict) -> str:
    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):                    # (journald stores non-UTF-8 text as bytes)
        msg = bytes(x & 0xFF for x in msg if isinstance(x, int)).decode("utf-8", "replace")
    return str(msg)


def parse(entries: list[dict], names: set[str], pids: set[int]) -> list[Event]:
    """Journal entries (``journalctl -o json`` objects) → what concerns the run. Kernel lines of one fault span
    several entries (amdgpu names the process on the next line), so a little context is kept."""
    out: list[Event] = []
    for i, e in enumerate(entries):
        msg = _message(e)
        at = int(e.get("__REALTIME_TIMESTAMP") or 0) / 1e6
        ident = e.get("SYSLOG_IDENTIFIER", "")
        if ident == "systemd-coredump" or e.get("COREDUMP_PID"):
            proc = str(e.get("COREDUMP_COMM") or "")
            pid = int(e["COREDUMP_PID"]) if str(e.get("COREDUMP_PID", "")).isdigit() else None
            if _ours(proc, pid, names, pids) or _ours(Path(str(e.get("COREDUMP_EXE") or "")).name, pid, names, pids):
                out.append(Event("core", proc, pid, at, str(e.get("COREDUMP_SIGNAL_NAME") or "a signal"),
                                 "core dump kept"))
            continue
        if ident == "systemd-oomd":
            m = _OOMD.search(msg)
            if m:                                  # (a cgroup, not a process: attributed by time)
                out.append(Event("oomd", "", None, at, "oomd", f"killed {Path(m.group(1)).name} ({m.group(2)})",
                                 ours=False))
            continue
        m = _OOM.search(msg)
        if m:
            pid, proc = int(m.group(1)), m.group(2)
            if _ours(proc, pid, names, pids):
                out.append(Event("oom", proc, pid, at, "oom-kill", "killed for memory",
                                 mem=int(m.group(3)) * 1024 if m.group(3) else 0))
            continue
        m = _XID.search(msg)
        if m:
            xid = int(m.group(1))
            what, _whose = XID.get(xid, (f"Xid {xid}", "driver"))
            pm = _XID_PROC.search(m.group(2) or "")
            proc = pm.group(2).strip() if pm else ""
            pid = int(pm.group(1)) if pm and pm.group(1).isdigit() else None
            named = bool(proc) and proc != "<unknown>"
            if not named or _ours(proc, pid, names, pids):
                out.append(Event("gpu", proc if named else "", pid, at, f"nvidia:{xid}", what, ours=named))
            continue
        m = _AMD.search(msg)
        if m:
            what = m.group(1).lower()
            near = " ".join(_message(x) for x in entries[i:i + 4])
            pm = _AMD_PROC.search(near)
            proc, pid = (pm.group(1), int(pm.group(2))) if pm else ("", None)
            if "succeeded" in what:
                continue                           # (the end of a reset already counted)
            code = "amdgpu:" + ("ring-timeout" if "timeout" in what else "page-fault" if "fault" in what else "reset")
            if (not proc or _ours(proc, pid, names, pids)) and not any(
                    x.kind == "gpu" and x.code == code and abs(x.at - at) < 5 for x in out):
                out.append(Event("gpu", proc, pid, at, code, what, ours=bool(proc)))
            continue
        m = _I915.search(msg)
        if m:
            pm = _I915_PROC.search(msg)
            proc = (pm.group(1) or pm.group(3)) if pm else ""
            pid_s = (pm.group(2) or pm.group(4)) if pm else None
            pid = int(pid_s) if pid_s else None
            if not proc or _ours(proc, pid, names, pids):
                out.append(Event("gpu", proc, pid, at, "intel:hang", m.group(1), ours=bool(proc)))
            continue
        m = _SEGV.search(msg)
        if m and e.get("_TRANSPORT", "kernel") == "kernel":
            proc, pid = m.group(1).strip(), int(m.group(2))
            if _ours(proc, pid, names, pids):
                how = m.group(3).split(" at ")[0]
                out.append(Event("segfault", proc, pid, at,
                                 next((sig for k, sig in _SIGNAL.items() if k in how), "SIGSEGV"), how,
                                 Path(m.group(4) or "").name))
    return out


def _journal(since: float, until: float, user: bool = False) -> tuple[list[dict], bool]:
    """The journal's kernel, systemd-oomd and systemd-coredump entries in the window → (entries, readable)."""
    exe = shutil.which("journalctl")
    if not exe:
        return [], False
    cmd = [exe, "--no-pager", "-o", "json", f"--since=@{since:.0f}", f"--until=@{until:.0f}"]
    cmd += ["--user", "SYSLOG_IDENTIFIER=systemd-coredump"] if user else \
        ["_TRANSPORT=kernel", "+", "SYSLOG_IDENTIFIER=systemd-oomd", "+", "SYSLOG_IDENTIFIER=systemd-coredump"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8, env={**os.environ, "SYSTEMD_COLORS": "0"})
    except (OSError, subprocess.SubprocessError):
        return [], False
    err = r.stderr.lower()
    # (as a user outside the journal's groups only the user's own journal is visible, and journalctl says so)
    readable = r.returncode == 0 and "insufficient permissions" not in err and \
        "not seeing messages from other users and the system" not in err
    out = []
    for ln in r.stdout.splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out, readable


def _dmesg(since: float, until: float) -> tuple[list[dict], bool]:
    """Without the journal (or without access to it): the kernel's own buffer, where the kernel lets users read it."""
    exe = shutil.which("dmesg")
    if not exe:
        return [], False
    try:
        r = subprocess.run([exe, "--time-format=raw", "--nopager"], capture_output=True, text=True, timeout=5)
        up = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], False
    if r.returncode != 0:
        return [], False
    boot = time.time() - up
    out = []
    for ln in r.stdout.splitlines():
        m = re.match(r"^\s*\[?\s*(\d+\.\d+)\]?\s*(.*)$", ln)
        if m and since <= boot + float(m.group(1)) <= until:
            out.append({"MESSAGE": m.group(2), "__REALTIME_TIMESTAMP": str(int((boot + float(m.group(1))) * 1e6)),
                        "_TRANSPORT": "kernel"})
    return out, True


def read(since: float, until: float, names: set[str], pids: set[int] | None = None) -> tuple[list[Event], bool]:
    """What the system logged about a run between ``since`` and ``until`` (wall-clock seconds). ``names``: the game's
    program names; ``pids``: its processes, when known. Returns (events, whether the system log could be read)."""
    if os.environ.get("EZGD_SYSLOG", "1") == "0":
        return [], True                            # (tests: never the real machine's journal)
    pids = set(pids or ())
    entries, readable = _journal(since - 2, until + 2)
    if not readable:
        k, readable = _dmesg(since - 2, until + 2)
        user, _ok = _journal(since - 2, until + 2, user=True)     # the user's own core dumps are there
        entries = k + user
    try:
        events = parse(entries, set(names), pids)
    except Exception:
        log.exception("reading the system log failed")
        return [], readable
    return events, readable


def markers(events: list[Event], readable: bool) -> str:
    """The lines appended to a run's log ("" when there is nothing to say)."""
    lines = [e.marker() for e in events]
    if not readable:
        lines.append(UNREADABLE + " the system log (kernel, graphics driver) could not be read on this computer")
    return "".join(ln + "\n" for ln in lines)


def from_text(text: str) -> tuple[list[Event], bool]:
    """The markers in a log → (events, whether the system log was readable for that run)."""
    out = []
    for m in re.finditer(r"^" + re.escape(MARK) + r"(\{.*\})\s*$", text, re.M):
        try:
            d = json.loads(m.group(1))
            out.append(Event(**{k: v for k, v in d.items() if k in Event.__dataclass_fields__}))
        except (ValueError, TypeError):
            continue
    return out, UNREADABLE not in text


def memory_facts() -> dict:
    """RAM and swap of this computer (for what to do after the kernel killed a game for memory)."""
    out = {}
    try:
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = ln.partition(":")
            if k in ("MemTotal", "SwapTotal", "SwapFree"):
                out[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["zram"] = any(Path("/sys/block").glob("zram*")) and any(
            "zram" in ln for ln in Path("/proc/swaps").read_text().splitlines()[1:])
    except OSError:
        out["zram"] = False
    return out


def run_names(exe: Path) -> set[str]:
    """The program names a game's processes have (its .exe, an Unreal game's *-Shipping.exe)."""
    from ezgd.platform.proton import game_binary_dirs
    names = {exe.name}
    try:
        for d in game_binary_dirs(exe):
            names |= {p.name for p in d.glob("*-Shipping.exe")}
    except OSError:
        pass
    return names
