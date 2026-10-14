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
