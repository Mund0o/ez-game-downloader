"""Monitors' real resolution from the desktop (a 3840×2160 monitor at 150 % showed up as 5120×2880), and couch
mode's "keep above" never staying on the app's other windows."""

import json

from PySide6.QtCore import QRect, Qt

from ezgd.ui import displays, window_control
from test_ui import ui  # noqa: F401 — the shared main-window fixture

KSCREEN = {"outputs": [{"name": "DP-1", "enabled": True, "scale": 1.5, "currentModeId": "2", "rotation": 1,
                        "modes": [{"id": "1", "size": {"width": 2560, "height": 1440}},
                                  {"id": "2", "size": {"width": 3840, "height": 2160}}]},
                       {"name": "HDMI-A-1", "enabled": False, "currentModeId": "", "modes": []},
                       {"name": "DP-2", "enabled": True, "currentModeId": "7", "rotation": 2,      # turned 90°
                        "modes": [{"id": "7", "size": {"width": 2560, "height": 1440}}]}]}

MUTTER = ("(uint32 3, [(('DP-1', 'DEL', 'DELL U2720Q', 'ABC'), [('3840x2160@60.000', 3840, 2160, 60.0, 2.0, "
          "[1.0, 1.25, 1.5, 1.75, 2.0], {'is-current': <true>, 'is-preferred': <true>}), ('2560x1440@59.951', 2560, "
          "1440, 59.951, 1.0, [1.0, 1.25], {})], {'is-builtin': <false>, 'display-name': <'Dell 27'>})], "
          "[(0, 0, 1.5, uint32 0, true, [('DP-1', 'DEL', 'DELL U2720Q', 'ABC')], @a{sv} {})], "
          "{'layout-mode': <uint32 1>})")

XRANDR = """Screen 0: minimum 320 x 200, current 5120 x 2880, maximum 16384 x 16384
DP-1 connected primary 5120x2880+0+0 (normal left inverted right x axis y axis) 698mm x 392mm
   3840x2160     59.98*+  29.98
   2560x1440     59.95
HDMI-1 disconnected (normal left inverted right x axis y axis)
"""


def test_the_desktop_tools_are_read_right():
    assert displays.from_kscreen(json.dumps(KSCREEN)) == {"DP-1": (3840, 2160), "DP-2": (1440, 2560)}
    assert displays.from_mutter(MUTTER) == {"DP-1": (3840, 2160)}
    assert displays.from_xrandr(XRANDR) == {"DP-1": (3840, 2160)}         # the mode, not the 2× framebuffer
    assert displays.from_kscreen("not json") == {} and displays.from_mutter("") == {}


class FakeScreen:
    def __init__(self, name, w, h, dpr):
        self._n, self._g, self._dpr = name, QRect(0, 0, w, h), dpr

    def name(self):
        return self._n

    def geometry(self):
        return self._g

    def devicePixelRatio(self):
        return self._dpr


def test_a_4k_monitor_at_150_percent_is_3840x2160_not_5120x2880(monkeypatch, qapp):
    screen = FakeScreen("DP-1", 2560, 1440, 2.0)            # what Qt says on the friend's PC: 150 % as "2"
    monkeypatch.setenv("EZGD_DISPLAYS", "1")                 # (the tests run on Qt's offscreen screens)
    monkeypatch.setattr(displays, "_modes", {})
    monkeypatch.setattr(displays, "_at", 1e12)               # (no background refresh in the test)
    assert window_control.real_size(screen) == (5120, 2880)  # Qt alone: wrong
    monkeypatch.setattr(displays, "_modes", {"DP-1": (3840, 2160)})
    assert window_control.real_size(screen) == (3840, 2160)
    assert abs(window_control.real_scale(screen) - 1.5) < 1e-9
    monkeypatch.setattr(displays, "_modes", {"DP1": (3840, 2160)})          # one monitor, another spelling
    assert window_control.real_size(screen) == (3840, 2160)


def test_leaving_couch_mode_releases_keep_above_everywhere(qtbot):
    from PySide6.QtWidgets import QWidget
    w = QWidget()
    qtbot.addWidget(w)
    w.show()
    w.windowHandle().setFlag(Qt.WindowStaysOnTopHint, True)
    window_control.release_all()
    assert not (w.windowHandle().flags() & Qt.WindowStaysOnTopHint)


def test_couch_fills_the_screen_when_full_screen_kept_the_old_size(ui, qtbot, monkeypatch):
    """A friend's couch mode came up the size of the desktop window although it was "full screen"."""
    from test_couch_ui import _open_couch
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    qtbot.wait(300)
    asked = []
    monkeypatch.setattr(couch, "width", lambda: 1000)              # what the window manager left it at
    monkeypatch.setattr(couch, "height", lambda: 600)
    monkeypatch.setattr(couch, "setGeometry", lambda g: asked.append(g))
    couch._size_tries = 0
    couch._fit_screen()
    assert asked and asked[0] == couch.screen().geometry()          # sized to the monitor, full screen again
    couch._fit_screen()
    couch._fit_screen()
    assert len(asked) == 2                                          # tried twice at most: never a loop
    monkeypatch.undo()
    couch.force_close()


def test_a_setting_changed_in_couch_mode_shows_on_the_desktop(ui, qtbot):
    from test_couch_ui import _open_couch
    from ezgd.ui.couch import T_SETTINGS
    win, core, _ = ui
    win.navigate("settings")
    box = win.pages["settings"].widgets["couch_sounds"]
    assert box.isChecked()
    couch = _open_couch(win, qtbot)
    couch._switch_tab(T_SETTINGS, force=True)
    page = couch.current_page()
    page._build()
    page.focus = next(k for k, it in enumerate(page.items) if it.label.startswith("Sounds ("))
    couch.zone = "main"
    couch.command("a")                                             # switched off in couch mode …
    qtbot.waitUntil(lambda: not box.isChecked(), timeout=5000)     # … and off on the desktop's Settings page
    couch.force_close()


def test_system_tools_get_the_systems_libraries_in_the_packaged_app(monkeypatch):
    """kscreen-doctor (a Qt program) did not start from the packaged app: it loaded the app's bundled Qt."""
    import sys
    from ezgd.platform.hostenv import host_env
    env = {"LD_LIBRARY_PATH": "/app/_internal", "PATH": "/usr/bin"}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert "LD_LIBRARY_PATH" not in host_env(env) and host_env(env)["PATH"] == "/usr/bin"
    env["LD_LIBRARY_PATH_ORIG"] = "/opt/lib"
    assert host_env(env)["LD_LIBRARY_PATH"] == "/opt/lib" and "LD_LIBRARY_PATH_ORIG" not in host_env(env)
    monkeypatch.setattr(sys, "frozen", False)
    assert host_env(env) == env                                    # running from source: untouched
