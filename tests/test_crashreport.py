"""Crash detection: copyable reports with stable error codes, for Python errors and hard crashes."""

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from ezgd.core import crashreport

CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {root!r})
    from pathlib import Path
    from ezgd.core import crashreport
    crashreport.install(Path({logs!r}))
    mode = {mode!r}
    if mode == "segv":
        import ctypes
        def load_game_list():
            ctypes.string_at(0)          # a hard crash (memory access error), like the GC race in Qt
        load_game_list()
    elif mode == "clean":
        crashreport.mark_clean_exit()
    elif mode == "killed":
        import os, signal
        os.kill(os.getpid(), signal.SIGKILL)
""")


def run_child(tmp_path: Path, mode: str) -> int:
    root = str(Path(__file__).resolve().parents[1])
    code = CHILD.format(root=root, logs=str(tmp_path / "logs"), mode=mode)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "ezgd.log").write_text("2026-10-11 INFO ezgd: download finished https://x.test/f?token=SECRET\n")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60).returncode


@pytest.fixture
def restore_hooks():
    hooks = (sys.excepthook, threading.excepthook)
    yield
    crashreport.set_callback(None)
    crashreport.mark_clean_exit()
    sys.excepthook, threading.excepthook = hooks


def test_hard_crash_is_reported_on_next_start(tmp_path, restore_hooks):
    assert run_child(tmp_path, "segv") != 0
    rep = crashreport.install(tmp_path / "logs")
    assert rep is not None and rep.fatal and rep.code.startswith("EZGD-SEGV-") and len(rep.code) == 16
    assert "load_game_list" in rep.summary and "segmentation fault" in rep.summary
    assert "Error code: " + rep.code in rep.text and "--- Crash trace ---" in rep.text
    assert "SECRET" not in rep.text and "download finished" in rep.text      # log tail, tokens removed
    assert rep.path.exists() and rep.path.parent.name == "crashes"
    # the same crash gives the same code (so reports from different people can be matched)
    crashreport.mark_clean_exit()
    assert run_child(tmp_path, "segv") != 0
    assert crashreport.install(tmp_path / "logs").code == rep.code


def test_hard_crash_report_has_the_native_trace(tmp_path, restore_hooks, monkeypatch):
    """The Python trace ends where Qt's C++ starts (EZGD-SEGV-F117EA named only the game page's exec()): the core
    dump systemd kept names the library and function. Matched by pid and time; the error code does not change."""
    assert run_child(tmp_path, "segv") != 0
    plain = crashreport.install(tmp_path / "logs").code
    crashreport.mark_clean_exit()
    assert run_child(tmp_path, "segv") != 0
    import json
    info = json.loads((tmp_path / "logs" / "session.json").read_text())
    at = int((tmp_path / "logs" / "faulthandler.log").stat().st_mtime * 1e6)
    stack = ("Process {pid} (ezgd) of user 1000 dumped core.\n\nModule libQt6Core.so.6 without build-id.\n"
             "Stack trace of thread {pid}:\n#0  0x0000000000000000 n/a (n/a + 0x0)\n"
             "#1  0x00007efc6e047ace _ZN24QWebEngineProfilePrivate17downloadRequestedERKN15QtWebEngineCore"
             " (libQt6WebEngineCore.so.6 + 0x5447ace)\n#2  0x00007efc659eb7f4 _ZN7QObject5eventEP6QEvent "
             "(libQt6Core.so.6 + 0x1eb7f4)\n\nStack trace of thread 99:\n#0  0x1 futex_wait (libc.so.6 + 0x1)\n")
    asked = []

    def journal(args):
        asked.append(args)
        if "-k" in args:
            return [{"MESSAGE": f"ezgd[{info['pid']}]: segfault at 0 ip 0 sp 7ffc error 14 likely on CPU 3"}]
        return [
            {"COREDUMP_PID": str(info["pid"]), "COREDUMP_TIMESTAMP": str(at - 400_000_000),     # same pid, a reboot
             "MESSAGE": "Process (someone else) dumped core.\nStack trace of thread 1:\n#0 0x1 n/a (other.so + 0x1)"},
            {"COREDUMP_PID": str(info["pid"]), "COREDUMP_TIMESTAMP": str(at), "COREDUMP_SIGNAL_NAME": "SIGSEGV",
             "COREDUMP_THREAD_NAME": "ezgd", "MESSAGE": list(stack.format(pid=info["pid"]).encode())}]
    monkeypatch.setenv("EZGD_SYSLOG", "1")
    monkeypatch.setattr(crashreport, "_journal", journal)
    rep = crashreport.install(tmp_path / "logs")
    assert rep.code == plain                                              # same crash, same code
    assert rep.summary.endswith(", in libQt6WebEngineCore.so.6.")
    native = rep.text.split("--- Native trace (systemd-coredump) ---\n", 1)[1]
    assert native.startswith("SIGSEGV in thread “ezgd”\nStack trace of thread")
    assert "downloadRequested" in native and "QObject5event" in native
    assert "other.so" not in native and "futex_wait" not in native       # only the crashing thread, only ours
    assert "kernel: ezgd[" in native and "segfault at 0" in native
    assert any(f"COREDUMP_PID={info['pid']}" in a for a in asked)
    # nothing in the journal (no systemd-coredump, no access): the report says so and still works
    crashreport.mark_clean_exit()
    assert run_child(tmp_path, "segv") != 0
    monkeypatch.setattr(crashreport, "_journal", lambda args: [])
    rep = crashreport.install(tmp_path / "logs")
    assert rep.code == plain and rep.summary.endswith("load_game_list.")
    assert "(none: the system kept no core dump record of it)" in rep.text


def test_clean_exit_and_killed_process(tmp_path, restore_hooks):
    run_child(tmp_path, "clean")
    assert crashreport.install(tmp_path / "logs") is None
    crashreport.mark_clean_exit()
    run_child(tmp_path, "killed")
    rep = crashreport.install(tmp_path / "logs")
    assert rep.code.startswith("EZGD-EXIT-") and "closed unexpectedly" in rep.summary


def test_python_errors_reach_the_callback_with_home_removed(tmp_path, restore_hooks):
    crashreport.install(tmp_path / "logs")
    got = []
    crashreport.set_callback(got.append)
    try:
        open(Path.home() / "no-such-dir" / "f.txt")
    except OSError:
        sys.excepthook(*sys.exc_info())
    t = threading.Thread(target=lambda: 1 / 0, name="extract-worker")
    t.start()
    t.join()
    assert [r.code[:8] for r in got] == ["EZGD-PY-", "EZGD-PY-"]
    assert "FileNotFoundError" in got[0].summary and str(Path.home()) not in got[0].text
    assert "ZeroDivisionError" in got[1].summary and "extract-worker" in got[1].summary
    assert len(crashreport.latest_reports(tmp_path / "logs")) == 2
    loaded = crashreport.load_report(crashreport.latest_reports(tmp_path / "logs")[0])
    assert loaded.code in {r.code for r in got}


def test_crash_window_copies_code_and_report(qapp, tmp_path, restore_hooks):
    from PySide6.QtGui import QGuiApplication
    from ezgd.ui.dialogs.crash import CrashDialog, CrashNotifier
    rep = crashreport.CrashReport("EZGD-SEGV-ABC123", "The app crashed.", "full report text", fatal=True)
    dlg = CrashDialog(rep)
    dlg.copy_code.click()
    assert QGuiApplication.clipboard().text() == "EZGD-SEGV-ABC123"
    dlg.copy_all.click()
    assert QGuiApplication.clipboard().text() == "full report text"
    assert dlg.code.text() == "EZGD-SEGV-ABC123"
    n = CrashNotifier(lambda: None)
    n._show(rep)
    n._show(rep)                       # a second report while one is open does not stack windows
    assert n._open is not None and n._counts["EZGD-SEGV-ABC123"] == 2
    n._open.close()


def test_repeated_error_saved_once(tmp_path, restore_hooks):
    crashreport.install(tmp_path / "logs")
    got = []
    crashreport.set_callback(got.append)
    for _ in range(50):                                   # e.g. an error in a paint event, every frame
        try:
            {}["missing"]
        except KeyError:
            sys.excepthook(*sys.exc_info())
    assert len(crashreport.latest_reports(tmp_path / "logs")) == 1
    assert [r.repeat for r in got].count(False) == 1 and len(got) == 50
