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
