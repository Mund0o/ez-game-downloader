"""Crashes and freezes while playing: what happened, kept before the next start overwrites it.

A test start lasts 45 seconds; a crash 20 minutes in, or a freeze, only ever happens while someone plays. So every
normal start records which DLLs were loaded from where (``+loaddll``, a few hundred lines), and when a play session
ends in a crash — the game's crash handler wrote a dump (Unreal, Unity, Sentry, Crashpad, Windows' own), Wine
reported an unhandled exception, or the exit code is a crash status — the app keeps, per game, in
``crashes/<id>/<time>/``: the session's log, the dumps, the engine's log, and ``record.json`` with the traced cause
(where it crashed, what was loaded, whose loader was replaced) and the computer it happened on.

A freeze leaves no dump. Its evidence is what every thread of the game was doing when it hung, read from Linux
itself (``/proc/<pid>/task``: the thread's name, state, the system call it waits in, what that call works on, how
much processor time it used in between): all threads waiting for each other (a deadlock), one thread busy at full
speed (an endless loop), a thread inside the graphics driver, a thread stuck in the kernel (disk or driver).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP = 10                       # records kept per game
NORMAL_EXITS = {0, None, -15, 143, -9, 137, -2, 130}      # quit, stopped by the app or the user


def folder(data: Path, entry_id: int) -> Path:
    return data / "crashes" / str(entry_id)


def records(data: Path, entry_id: int) -> list[dict]:
    """This game's records, newest first."""
    out = []
    root = folder(data, entry_id)
    if root.is_dir():
        for d in sorted(root.iterdir(), reverse=True):
            try:
                rec = json.loads((d / "record.json").read_text("utf-8"))
                rec["folder"] = str(d)
                out.append(rec)
            except (OSError, ValueError):
                continue
    return out


def latest(data: Path, entry_id: int, since: float = 0.0) -> dict | None:
    rec = next(iter(records(data, entry_id)), None)
    return rec if rec and rec.get("at", 0) >= since else None


def is_crash_exit(code: int | None) -> bool:
    if code in NORMAL_EXITS:
        return False
    from ezgd.library import history
    ex = history.explain_exit(code)
    return bool(ex) and ex[0] in ("crash", "cpu", "heap", "failfast", "dll", "dllinit", "killed", "memory", "dotnet")


# ------------------------------------------------------------------------------- freezes
_SYSCALLS = {0: "read", 1: "write", 7: "poll", 16: "ioctl", 23: "select", 35: "nanosleep", 202: "futex",
             230: "clock_nanosleep", 232: "epoll_wait", 270: "pselect6", 271: "ppoll", 281: "epoll_pwait",
             441: "epoll_pwait2", 61: "wait4", 17: "pread64", 18: "pwrite64", 72: "fcntl", 74: "fsync"}


def _ticks(task: Path) -> int:
    try:
        f = (task / "stat").read_text().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])
    except (OSError, ValueError, IndexError):
        return 0


def _thread(pid: int, task: Path) -> dict:
    t: dict = {"tid": int(task.name)}
    try:
        t["name"] = (task / "comm").read_text().strip()
        t["state"] = (task / "stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return t
    try:
        t["wchan"] = (task / "wchan").read_text().strip()
    except OSError:
        t["wchan"] = ""
    try:
        sc = (task / "syscall").read_text().split()
        if sc and sc[0].lstrip("-").isdigit() and int(sc[0]) >= 0:
            t["syscall"] = _SYSCALLS.get(int(sc[0]), f"#{sc[0]}")
            if t["syscall"] in ("ioctl", "read", "write", "pread64", "pwrite64") and len(sc) > 1:
                try:
                    t["on"] = os.readlink(f"/proc/{pid}/fd/{int(sc[1], 16)}")
                except (OSError, ValueError):
                    pass
        elif sc and sc[0] == "running":
            t["syscall"] = "running"
    except OSError:
        pass
    return t


def _kind(t: dict) -> str:
    on = t.get("on", "")
    if t.get("busy"):
        return "busy"
    if t.get("state") == "D":
        return "kernel"
    if t.get("syscall") == "ioctl" and ("/dev/nvidia" in on or "/dev/dri" in on or "/dev/kfd" in on):
        return "gpu"
    if t.get("syscall") == "futex" or (t.get("syscall") == "ioctl" and "ntsync" in on):
        return "waiting"                      # (ntsync: Proton's Windows waits — WaitForSingleObject and friends)
    if t.get("syscall") in ("poll", "ppoll", "select", "pselect6", "epoll_wait", "epoll_pwait", "epoll_pwait2",
                            "read") and "socket" in on:
        return "network"
    if t.get("syscall") in ("poll", "ppoll", "select", "pselect6", "epoll_wait", "epoll_pwait", "epoll_pwait2",
                            "read"):
        return "events"
    if t.get("syscall") in ("nanosleep", "clock_nanosleep"):
        return "sleeping"
    return "other"


def snapshot(pids: list[int], interval: float = 1.5) -> dict:
    """What every thread of the game's Windows processes is doing (two looks ``interval`` apart: which threads
    used the processor in between)."""
    procs = []
    for pid in pids:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ").lower()
        except OSError:
            continue
        if ".exe" in cmd and not any(x in cmd for x in ("wineserver", "services.exe", "winedevice.exe", "plugplay",
                                                         "svchost.exe", "explorer.exe", "rpcss.exe", "tabtip.exe",
                                                         "xalia.exe", "steam.exe", "umu.exe")):
            procs.append(pid)
    first: dict[tuple[int, int], int] = {}
    for pid in procs:
        for task in Path(f"/proc/{pid}/task").glob("*"):
            first[(pid, int(task.name))] = _ticks(task)
    time.sleep(interval)
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    threads = []
    for pid in procs:
        for task in Path(f"/proc/{pid}/task").glob("*"):
            t = _thread(pid, task)
            used = (_ticks(task) - first.get((pid, t["tid"]), _ticks(task))) / hz
            t["cpu"] = round(used / interval * 100)
            t["busy"] = t["cpu"] >= 90           # (a working game's render thread runs at ~60 %)
            t["pid"] = pid
            t["kind"] = _kind(t)
            threads.append(t)
    return {"at": time.time(), "threads": threads, "verdict": verdict(threads)}


def verdict(threads: list[dict]) -> str:
    """The freeze in words."""
    if not threads:
        return "The game's threads could not be read."
    by: dict[str, list[dict]] = {}
    for t in threads:
        by.setdefault(t["kind"], []).append(t)
    names = lambda ts: ", ".join(sorted({t.get("name") or str(t["tid"]) for t in ts})[:4])   # noqa: E731
    if by.get("busy"):
        return (f"{len(by['busy'])} thread(s) were working at full speed ({names(by['busy'])}) while the game did not "
                "react — stuck in an endless loop (or an extremely long load).")
    if by.get("kernel"):
        return (f"{names(by['kernel'])} was stuck inside the Linux kernel (state D) — waiting for the disk or a "
                "driver. A slow or failing drive, a network drive or an NTFS/exFAT partition are the usual causes.")
    if by.get("gpu"):
        return (f"{names(by['gpu'])} was waiting inside the graphics driver and nothing else was working — the GPU or "
                "its driver hung (a driver update, another Proton, or lower graphics settings help most often).")
    waiting = len(by.get("waiting", []))
    if waiting and waiting >= len(threads) * 0.5:
        return (f"Every thread was waiting ({waiting} of {len(threads)} for another thread) and none was working — "
                "a deadlock: the game's threads waited for each other.")
    return "Nothing was working: " + ", ".join(f"{len(v)} {k}" for k, v in sorted(by.items())) + "."


# ------------------------------------------------------------------------------- the record
def capture(data: Path, entry, prefix_dir: Path, log_path: Path, code: int | None, played: int, *,
            kind: str = "crash", freeze: dict | None = None, machine: dict | None = None) -> dict | None:
    """Keep what happened in the play session that just ended — None when it was not a crash (nor a freeze)."""
    from ezgd.library import tracer
    from ezgd.library.diagnose import engine_logs, engine_of, last_run
    from ezgd.library.minidump import find_dumps, read_minidump
    exe = Path(entry.executable or "")
    started = time.time() - played - 30
    run = last_run(log_path)
    dumps = []
    for p in find_dumps(Path(entry.install_path or exe.parent), prefix_dir, since=started):
        d = read_minidump(p)
        if d is not None and d.code is not None:
            dumps.append((p, d))
    text = run.text or ""
    if dumps:
        text += "\n" + tracer.dump_line(dumps[0][1])
    t = tracer.trace(text, exe, exit_code=code, seconds=played)
    # (a game the kernel killed for memory exits with SIGKILL, like one the app stopped: the system log tells them
    # apart — ezgd.library.syslog)
    crashed = kind == "freeze" or bool(dumps) or (t.crash is not None and t.crash.certain) or is_crash_exit(code) \
        or bool(t.serious())
    if not crashed:
        return None
    engine = engine_of(exe) if exe.name else None
    eng_name, eng_text = engine_logs(exe, prefix_dir, engine) if exe.name else ("", "")
    try:
        if eng_name and Path(eng_name).stat().st_mtime < started:
            eng_name, eng_text = "", ""
    except OSError:
        eng_name, eng_text = "", ""
    chain, _sus = tracer.follow(t, entry)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = folder(data, entry.id) / stamp
    out.mkdir(parents=True, exist_ok=True)
    keep = text if len(text) < 2_000_000 else text[:600_000] + "\n…\n" + text[-1_400_000:]
    (out / "session.log").write_text(keep, "utf-8")
    if eng_text:
        (out / "engine.log").write_text(eng_text[-400_000:], "utf-8")
    try:                                          # what the game wrote into its own log files (any engine)
        from ezgd.library.manager import run_logs
        game_logs = run_logs(exe, prefix_dir, started) if exe.name else ""
    except Exception:
        game_logs = ""
    if game_logs:
        (out / "game-logs.txt").write_text(game_logs[-600_000:], "utf-8")
    copied = []
    for p, _d in dumps[:3]:
        try:
            if p.stat().st_size <= 64 << 20:
                shutil.copy2(p, out / p.name)
                copied.append(p.name)
        except OSError:
            continue
    c = t.crash
    rec = {"kind": kind, "at": time.time(), "played": played, "code": code, "game": entry.title, "engine": engine,
           "crash": None if c is None else {"code": c.code, "what": c.what, "module": c.module, "offset": c.offset,
                                             "kind": c.kind, "null": c.null, "describe": c.describe()},
           "dumps": copied, "dump_source": [str(p) for p, _d in dumps[:3]], "engine_log": eng_name,
           "phase": t.phase, "chain": chain, "replaced": [r.name for r in t.replaced],
           "fingerprint": tracer.fingerprint(t, engine), "machine": machine or {}, "freeze": freeze,
           "system": [e.describe() for e in t.serious()]}
    (out / "record.json").write_text(json.dumps(rec, indent=1, default=str), "utf-8")
    for old in sorted(folder(data, entry.id).iterdir(), reverse=True)[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    rec["folder"] = str(out)
    log.info("%s: %s after %ss kept in %s — %s", entry.title, kind, played, out,
             rec["crash"]["describe"] if rec["crash"] else (rec["system"] or [(freeze or {}).get("verdict", "")])[0])
    return rec


def summary(rec: dict) -> str:
    """One sentence for a notification / the Diagnose window."""
    mins = max(1, int(rec.get("played") or 0) // 60)
    if rec.get("kind") == "freeze":
        return f"froze after {mins} min — " + ((rec.get("freeze") or {}).get("verdict") or "")
    system = rec.get("system") or []
    if system and (rec.get("code") in NORMAL_EXITS or not rec.get("crash")):
        return f"stopped after {mins} min: {system[0]}"          # (the kernel / driver's account is the deeper one)
    c = rec.get("crash")
    if c:
        return f"crashed after {mins} min: {c['describe']}" + (f" ({system[0]})" if system else "")
    return f"crashed after {mins} min (exit code {rec.get('code')})"


def evidence(rec: dict) -> dict | None:
    """A record as evidence for the investigator (like a test run's)."""
    try:
        text = Path(rec["folder"], "session.log").read_text("utf-8", "replace")
    except (OSError, KeyError):
        return None
    extra = ""
    for name in ("engine.log", "game-logs.txt"):
        try:
            extra += "\n" + Path(rec["folder"], name).read_text("utf-8", "replace")
        except OSError:
            pass
    return {"text": text, "exit_code": rec.get("code") if rec.get("code") is not None else 1,
            "seconds": rec.get("played"), "label": "your play session", "overrides": {}, "extra": extra,
            "record": rec.get("folder")}
