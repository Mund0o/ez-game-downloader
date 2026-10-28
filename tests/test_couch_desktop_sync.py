"""Couch mode and the desktop window together: what changes on one side shows on the other."""

import sys

import pytest

from test_couch_ui import _open_couch
from test_quick_menu import _running
from test_ui import ui  # noqa: F401 — the shared main-window fixture

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process states from /proc")


@linux_only
def test_a_suspended_game_shows_on_the_desktop_with_resume(ui, qtbot, tmp_path):
    win, core, _ = ui
    a = _running(win, core, tmp_path, qtbot, "Desk Freeze")
    lib = win.pages["library"]
    win.navigate("library")
    lib.select(a.id)
    lib._suspend(lib.entries[a.id])                                  # More → Suspend (freeze) the game
    qtbot.waitUntil(lambda: a.id in lib.suspended, timeout=5000)
    assert lib._status_text(lib.entries[a.id]) == "Suspended"
    lib._render(a.id)
    from PySide6.QtWidgets import QPushButton

    def texts():                       # (a new page's buttons show a moment later, the old ones go a moment later:
        return [b.text().strip() for b in lib.findChildren(QPushButton) if b.isVisible()]    # read once settled)
    qtbot.waitUntil(lambda: "Resume" in texts() and "Stop" in texts(), timeout=5000)
    couch = _open_couch(win, qtbot)                                  # couch mode knows it is suspended
    assert a.id in couch.suspended and couch.state_text(next(e for e in couch.entries if e.id == a.id)) == \
        "Suspended"
    couch.force_close()                                              # (couch mode leaving lets it run again)
    qtbot.waitUntil(lambda: a.id not in lib.suspended, timeout=5000)
    win.bridge.run_sync(core.library_call("stop_completely", a.id, 3))


def test_settings_and_the_chosen_game_go_both_ways(ui, qtbot, tmp_path):
    win, core, _ = ui
    folders = []
    for name in ("Sync One", "Sync Two"):
        f = tmp_path / "games" / name
        f.mkdir(parents=True)
        (f / "Game.exe").write_bytes(b"MZ" + b"\0" * 100)
        folders.append(win.bridge.run_sync(core.library_call("import_folder", name, str(f))))
    win._reload_library()
    win.navigate("library")
    lib = win.pages["library"]
    qtbot.waitUntil(lambda: len(lib.entries) == 2, timeout=5000)
    lib.select(folders[1].id)                                        # chosen on the desktop …
    couch = _open_couch(win, qtbot)
    from ezgd.ui.couch import T_HOME
    assert couch._items(T_HOME)[couch.index[T_HOME]].id == folders[1].id      # … chosen in couch mode
    s1 = couch._s()
    win.bridge.run_sync(core.set_setting("couch_ui_scale", 125))     # changed elsewhere while couch mode is open
    qtbot.waitUntil(lambda: abs(couch._s() - s1) > 0.01, timeout=5000)
    couch.focus_entry(folders[0].id)                                 # moved in couch mode …
    couch.force_close()
    qtbot.waitUntil(lambda: lib.current_id == folders[0].id, timeout=5000)    # … chosen on the desktop
    win.bridge.run_sync(core.set_setting("couch_ui_scale", 100))
