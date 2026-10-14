"""Crash detection and copyable crash reports.

Two kinds of failure are caught:

* **Python errors** that nothing handled (in the GUI, in Qt callbacks or in worker threads) — reported at once,
  while the app keeps running.
* **Hard crashes** that kill the whole process (segmentation fault, abort, the system killing the app). Nothing
  can be shown at that moment, so :mod:`faulthandler` writes every thread's Python stack to a file as the process
  dies, and a marker file shows the session did not end normally. The next start turns that into a report.

Every report has a short error code (``EZGD-SEGV-3F2A91``) that is the same for the same crash, so reports from
different people can be matched, and a full text (what happened, versions, the stack, the last log lines) that can
be copied. Home folder paths and URL tokens are removed from it.
"""

from __future__ import annotations

import faulthandler
import hashlib
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

KEEP_REPORTS = 20
_SIGNALS = {"Segmentation fault": "SEGV", "Aborted": "ABRT", "Fatal Python error: Aborted": "ABRT",
            "Floating point exception": "FPE", "Bus error": "BUS", "Illegal instruction": "ILL"}
_STATE: dict = {}


@dataclass
class CrashReport:
    code: str                 # EZGD-<kind>-<6 hex>
    summary: str              # one sentence for people
    text: str                 # the full report to copy
    path: Path | None = None  # where it was saved
    fatal: bool = False       # the app died (reported on the next start)
    repeat: bool = False      # the same error again within a minute (not saved again)


# ------------------------------------------------------------------ helpers
def _redact(text: str) -> str:
    from ezgd.core.logging_setup import redact
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    user = os.environ.get("USER") or os.environ.get("USERNAME")
    if user and len(user) > 2:
        text = re.sub(rf"(?<![\w]){re.escape(user)}(?![\w])", "<user>", text)
    return redact(text)


def _code(kind: str, signature: str) -> str:
    return f"EZGD-{kind}-{hashlib.sha1(signature.encode()).hexdigest()[:6].upper()}"


def _versions() -> str:
    from ezgd import __version__
    bits = [f"EZ Game Downloader {__version__}" + (" (packaged build)" if getattr(sys, "frozen", False) else ""),
            f"Python {platform.python_version()}"]
    try:
        import PySide6
        from PySide6 import QtCore
        bits.append(f"Qt {QtCore.qVersion()} / PySide6 {PySide6.__version__}")
    except Exception:
        pass
    try:
        import libtorrent
        bits.append(f"libtorrent {libtorrent.__version__}")
    except Exception:
        pass
    return " · ".join(bits)


def _system() -> str:
    name = platform.system()
    if name == "Linux":
        distro = ""
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip('"')
        except OSError:
            pass
        session = os.environ.get("XDG_SESSION_TYPE", "?")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "?")
        return f"Linux {platform.release()} ({distro or 'unknown distribution'}) · {desktop} on {session}"
    return f"{name} {platform.release()} ({platform.version()})"


def _log_tail(log_file: Path, lines: int = 80) -> str:
    try:
        with open(log_file, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return "(no log)"
    return "\n".join(data.splitlines()[-lines:])


def _ours(frames: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The app's own frames (else anything that is not the standard library or a dependency)."""
    app = [f for f in frames if "ezgd" in f[0].replace("\\", "/")]
    if app:
        return app
    lib = re.compile(r"(site-packages|dist-packages|[/\\]lib[/\\]python\d|<frozen )")
    return [f for f in frames if not lib.search(f[0])] or frames


def _frames_signature(frames: list[tuple[str, str]]) -> str:
    """Stable signature from (file name, function) pairs — line numbers change between versions."""
    return "|".join(f"{Path(f).name}:{fn}" for f, fn in frames[:4])


def _compose(code: str, summary: str, trace: str, log_file: Path | None, extra: str = "") -> str:
    parts = ["EZ Game Downloader crash report", f"Error code: {code}", f"What happened: {summary}",
             f"App: {_versions()}", f"System: {_system()}", f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    if extra:
        parts.append(extra)
    parts += ["", "--- Crash trace ---", trace.strip() or "(no trace)"]
    if log_file is not None:
        parts += ["", "--- Last log lines ---", _log_tail(log_file)]
    text = _redact("\n".join(parts))
    return text if len(text) < 60_000 else text[:30_000] + "\n…\n" + text[-25_000:]


def _save(report: CrashReport, folder: Path) -> CrashReport:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"crash-{time.strftime('%Y%m%d-%H%M%S')}-{report.code}.txt"
        p.write_text(report.text, encoding="utf-8")
        report.path = p
        for old in sorted(folder.glob("crash-*.txt"))[:-KEEP_REPORTS]:
            old.unlink(missing_ok=True)
    except OSError:
        log.exception("could not save the crash report")
    return report


# --------------------------------------------------------- python errors
def report_exception(exc_type, exc, tb, where: str = "") -> CrashReport:
    folder = _STATE.get("folder")
    frames = [(f.filename, f.name) for f in traceback.extract_tb(tb)] if tb else []
    ours = _ours(frames)
    code = _code("PY", f"{exc_type.__name__}|{_frames_signature(list(reversed(ours)))}")
    last = ours[-1] if ours else None
    place = f" in {Path(last[0]).stem}.{last[1]}" if last else ""
    message = str(exc).strip().splitlines()[0][:160] if str(exc).strip() else ""
    summary = f"{exc_type.__name__}{place}" + (f": {message}" if message else "") + (f" ({where})" if where else "")
    trace = "".join(traceback.format_exception(exc_type, exc, tb))
    report = CrashReport(code, _redact(summary), _compose(code, summary, trace, _STATE.get("log_file")))
    recent: dict = _STATE.setdefault("recent", {})
    now = time.monotonic()
    if now - recent.get(code, -1e9) < 60:
        report.repeat = True                     # the same error again (e.g. every repaint): counted, not re-saved
        return report
    recent[code] = now
    log.error("unhandled error %s: %s\n%s", code, summary, trace)
    return _save(report, folder) if folder else report


def _dispatch(report: CrashReport) -> None:
    cb: Callable[[CrashReport], None] | None = _STATE.get("callback")
    if cb is not None:
        try:
            cb(report)
        except Exception:
            log.exception("crash report callback failed")


def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _dispatch(report_exception(exc_type, exc, tb))


def _thread_hook(args: threading.ExceptHookArgs):
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "a background thread"
    _dispatch(report_exception(args.exc_type, args.exc_value, args.exc_traceback, f"thread “{name}”"))


# ---------------------------------------------------------- hard crashes
def parse_fault_log(text: str) -> tuple[str, list[tuple[str, str]], str]:
    """(kind, frames of the crashing thread innermost first, thread line) from a faulthandler dump."""
    kind = "CRASH"
    for k, v in _SIGNALS.items():
        if k in text:
            kind = v
            break
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Current thread")), None)
    frames: list[tuple[str, str]] = []
    thread = ""
    if start is not None:
        thread = lines[start].rstrip(":")
        for ln in lines[start + 1:]:
            m = re.match(r'\s+File "([^"]+)", line \d+ in (\S+)', ln)
            if not m:
                if ln.strip() == "" or ln.startswith(("Thread ", "Current thread", "Extension modules")):
                    break
                continue
            frames.append((m.group(1), m.group(2)))
    return kind, frames, thread


def _describe_fatal(kind: str, frames: list[tuple[str, str]]) -> str:
    what = {"SEGV": "crashed (segmentation fault — memory access error)", "ABRT": "aborted (a fatal error in Qt "
            "or a library)", "FPE": "crashed (arithmetic error)", "BUS": "crashed (bus error)",
            "ILL": "crashed (illegal instruction)", "EXIT": "closed unexpectedly"}.get(kind, "crashed")
    ours = _ours(frames)
    top = ours[0] if ours else None
    where = f" while running {Path(top[0]).stem}.{top[1]}" if top else ""
    return f"The app {what}{where}."


def _previous_session(state_dir: Path, fault_file: Path, log_file: Path, folder: Path) -> CrashReport | None:
    marker = state_dir / "session.json"
    if not marker.exists():
        return None
    try:
        info = json.loads(marker.read_text())
    except (OSError, ValueError):
        info = {}
    try:
        fault = fault_file.read_text("utf-8", "replace") if fault_file.exists() else ""
    except OSError:
        fault = ""
    ran = ""
    if info.get("started"):
        ran = f"Session: started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info['started']))}" + (
            f", app {info['version']}" if info.get("version") else "")
    # the previous session's log is the rotated one if this start already rotated it; the tail is enough
    if fault.strip():
        kind, frames, _thread = parse_fault_log(fault)
        code = _code(kind, f"{kind}|{_frames_signature(frames)}")
        summary = _describe_fatal(kind, frames)
        text = _compose(code, summary, fault, log_file, ran)
    else:
        code = _code("EXIT", "no-trace")
        summary = ("The app closed unexpectedly without leaving a crash trace — it was force-closed, the system ran "
                   "out of memory and stopped it, or the computer shut down.")
        text = _compose(code, summary, "(no trace — the process was stopped from outside)", log_file, ran)
    return _save(CrashReport(code, summary, text, fatal=True), folder)


def install(log_dir: Path, state_dir: Path | None = None) -> CrashReport | None:
    """Start crash detection. Returns the report of a crash in the previous session, if there was one."""
    state_dir = state_dir or log_dir
    folder = log_dir / "crashes"
    fault_file = log_dir / "faulthandler.log"
    log_file = log_dir / "ezgd.log"
    _STATE.update(folder=folder, log_file=log_file, state_dir=state_dir)
    previous = None
    try:
        previous = _previous_session(state_dir, fault_file, log_file, folder)
    except Exception:
        log.exception("could not read the previous session's crash data")
    if previous is not None:
        log.warning("the previous session crashed: %s — %s (report: %s)", previous.code, previous.summary,
                    previous.path)
    try:
        fh = open(fault_file, "w", encoding="utf-8")
        _STATE["fault_fh"] = fh
        faulthandler.enable(file=fh, all_threads=True)
    except OSError:
        log.exception("could not enable the crash trace")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        from ezgd import __version__
        (state_dir / "session.json").write_text(json.dumps({"pid": os.getpid(), "started": time.time(),
                                                            "version": __version__}))
    except OSError:
        log.exception("could not write the session marker")
    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    return previous


def set_callback(cb: Callable[[CrashReport], None] | None) -> None:
    """Where in-session error reports go (the GUI shows them)."""
    _STATE["callback"] = cb


def mark_clean_exit() -> None:
    """The session ended normally: no crash report on the next start."""
    state_dir: Path | None = _STATE.get("state_dir")
    if state_dir is not None:
        (state_dir / "session.json").unlink(missing_ok=True)
    try:
        faulthandler.disable()
        fh = _STATE.pop("fault_fh", None)
        if fh is not None:
            fh.close()
            Path(fh.name).write_text("")
    except (OSError, ValueError):
        pass


def latest_reports(log_dir: Path, limit: int = 10) -> list[Path]:
    folder = log_dir / "crashes"
    return sorted(folder.glob("crash-*.txt"), reverse=True)[:limit] if folder.is_dir() else []


def load_report(path: Path) -> CrashReport:
    text = path.read_text("utf-8", "replace")
    code = re.search(r"^Error code: (\S+)", text, re.M)
    what = re.search(r"^What happened: (.+)$", text, re.M)
    return CrashReport(code.group(1) if code else path.stem, what.group(1) if what else "", text, path)
