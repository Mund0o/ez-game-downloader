"""A frozen UI is logged with its length, where it happened and the line it was stuck in."""
import logging
import time

from PySide6.QtWidgets import QApplication


def stuck_in_the_downloads_page():
    time.sleep(0.6)                           # (the main thread blocked: what the log must name)


def test_a_stall_is_logged_with_where_and_the_stuck_line(qtbot, caplog):
    from ezgd.ui.stalls import StallWatch
    caplog.set_level(logging.INFO, logger="ezgd.ui.stalls")
    w = StallWatch(lambda: "the downloads page")
    w.start()
    try:
        qtbot.wait(300)
        stuck_in_the_downloads_page()
        deadline = time.monotonic() + 3
        while "UI stall" not in caplog.text and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
    finally:
        w.stop()
    assert "UI stall of" in caplog.text and "on the downloads page" in caplog.text
    assert "stuck_in_the_downloads_page" in caplog.text
    n = caplog.text.count("UI stall")
    qtbot.wait(400)                            # (a quiet UI writes nothing)
    assert caplog.text.count("UI stall") == n


def test_opening_the_downloads_sections_is_logged(ui, qtbot, caplog):  # noqa: F811
    """Every visit to the Downloads page and couch mode's Downloads tab leaves a line in ezgd.log: how many downloads
    and how long it took to show — so the log alone tells whether and where it lags."""
    from ezgd.ui import couch as C
    from test_couch_ui import _open_couch
    caplog.set_level(logging.INFO)
    win, core, _ = ui
    win.navigate("downloads")
    qtbot.waitUntil(lambda: "Downloads page opened: 0 download(s)" in caplog.text, timeout=5000)
    assert "drawn in" in caplog.text
    couch = _open_couch(win, qtbot)
    couch._switch_tab(C.T_DOWNLOADS)
    couch.update()
    qtbot.waitUntil(lambda: "couch Downloads tab opened: 0 download(s)" in caplog.text, timeout=5000)


from test_ui import ui  # noqa: E402,F401
