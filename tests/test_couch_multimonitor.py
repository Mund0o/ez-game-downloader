"""Couch mode with two monitors of different sizes (Qt's offscreen platform, configured with two screens)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

HERE = Path(__file__).parent
INNER = os.environ.get("EZGD_TWO_SCREENS") == "1"

if INNER:
    from test_ui import ui  # noqa: F401 — the shared main-window fixture
    from test_couch_ui import _open_couch


@pytest.mark.skipif(INNER or sys.platform == "win32", reason="runs the check below with two screens")
def test_couch_fills_the_monitor_it_is_on():
    """A friend's couch mode left part of a bigger monitor uncovered: on Wayland the compositor decides which
    monitor a full-screen window goes to, and couch mode forced the first monitor's size onto it."""
    env = {**os.environ, "EZGD_TWO_SCREENS": "1",
           "QT_QPA_PLATFORM": f"offscreen:configfile={HERE / 'fixtures' / 'two_screens.json'}"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(Path(__file__)),
                        "-k", "two_screens_inner"], cwd=HERE.parent, env=env, capture_output=True, text=True,
                       timeout=240)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "1 passed" in r.stdout


@pytest.mark.skipif(not INNER, reason="only inside the two-screen run")
def test_two_screens_inner(ui, qtbot):
    win, core, _ = ui
    screens = QApplication.screens()
    assert len(screens) == 2
    couch = _open_couch(win, qtbot)
    big = next(s for s in screens if s.name() == "BIG")
    couch.showNormal()                                         # the compositor put it on the bigger monitor
    couch.setScreen(big)
    couch.move(big.geometry().topLeft())
    couch.showFullScreen()
    qtbot.wait(100)
    assert couch.geometry() == big.geometry()
    couch._fit_screen()
    qtbot.wait(100)
    assert couch.screen() is big and couch.geometry() == big.geometry()     # the first monitor's size is not forced
    couch.actions.settings["couch_screen"] = "LEFT"            # a screen chosen in Settings: it goes there
    couch._fit_screen()
    qtbot.wait(100)
    assert couch.screen().name() == "LEFT" and couch.isFullScreen()
    couch.actions.settings["couch_screen"] = ""
    couch.force_close()
