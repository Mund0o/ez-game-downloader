"""GPU acceleration: couch mode drawn by OpenGL, its fallback to the CPU, and the desktop's start-up switches.

Qt's offscreen platform cannot show a QOpenGLWidget, but it still makes real OpenGL contexts (through the X
display): couch mode's frames are drawn with Qt's OpenGL paint engine into a framebuffer, exactly as
CouchGLWindow.paintGL draws them, and compared with the CPU's picture."""

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

from test_couch_intro import intro_on  # noqa: F401 — couch mode with its intro switched on
from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.ui import couch_intro, gpu


def _diff(a: QImage, b: QImage) -> float:
    """Average difference per colour channel (0–255) of two pictures, compared at 320×180."""
    a = a.convertToFormat(QImage.Format_RGB32).scaled(320, 180)
    b = b.convertToFormat(QImage.Format_RGB32).scaled(320, 180)
    tot = 0
    for y in range(a.height()):
        for x in range(a.width()):
            pa, pb = a.pixel(x, y), b.pixel(x, y)
            tot += sum(abs(((pa >> k) & 255) - ((pb >> k) & 255)) for k in (0, 8, 16))
    return tot / (a.width() * a.height() * 3)


def _lit(img: QImage) -> float:
    """How much of the picture is brighter than the dark background (0–1)."""
    small = img.convertToFormat(QImage.Format_RGB32).scaled(160, 90)
    n = sum(1 for y in range(90) for x in range(160) if small.pixelColor(x, y).lightness() > 90)
    return n / (160 * 90)


class GL:
    """An OpenGL framebuffer the size of the couch window, drawn into by couch mode's own frame code."""

    def __init__(self, couch):
        from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
        from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLPaintDevice
        self.ctx = QOpenGLContext()
        if not self.ctx.create():
            pytest.skip("no OpenGL on this computer")
        self.surf = QOffscreenSurface()
        self.surf.setFormat(self.ctx.format())
        self.surf.create()
        if not self.ctx.makeCurrent(self.surf):
            pytest.skip("no OpenGL on this computer")
        fmt = QOpenGLFramebufferObjectFormat()
        fmt.setSamples(4)
        fmt.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
        self.fbo = QOpenGLFramebufferObject(couch.width(), couch.height(), fmt)
        self.dev = QOpenGLPaintDevice(couch.width(), couch.height())

    def draw(self, couch, rect=None) -> QImage:
        self.ctx.makeCurrent(self.surf)
        self.fbo.bind()
        couch._dirty = True
        couch.gpu_paint = True
        try:
            couch._render(rect or couch.rect(), self.dev)
        finally:
            del couch.gpu_paint                   # (back to the class's False)
        assert not couch._paint_failed
        return self.fbo.toImage()


def test_couch_drawn_by_opengl_looks_like_the_cpu_picture(ui, qtbot):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    qtbot.wait(300)
    gl = GL(couch)
    from ezgd.ui.couch import TABS
    for tab in range(len(TABS)):                 # every page: text, rounded clip paths, cached layers, background
        couch._switch_tab(tab)
        qtbot.wait(200)
        cpu = couch.grab().toImage()
        assert _diff(cpu, gl.draw(couch)) < 2.0, TABS[tab]
    couch._switch_tab(0)
    couch.toasts.show("A message", "Fading in over couch mode", "success")      # see-through layers
    couch.show_overlay("A question", "On top of a dimmed page", [("OK", lambda: None, "primary")])
    qtbot.wait(400)
    assert couch.overlay is not None
    cpu = couch.grab().toImage()
    assert _diff(cpu, gl.draw(couch)) < 2.0
    # a partial frame redraws only its part: everything else stays as the last frame left it
    before = gl.draw(couch)
    couch.overlay = None
    from PySide6.QtCore import QRect
    after = gl.draw(couch, QRect(0, 0, 200, 120))
    assert after.copy(400, 200, 300, 300) == before.copy(400, 200, 300, 300)
    couch.force_close()


@pytest.mark.skipif(not couch_intro.VIDEO.is_file(), reason="intro video not built")
def test_the_intro_video_is_drawn_by_opengl(intro_on, qtbot):
    win, _core, _ = intro_on
    win.open_couch()
    qtbot.waitUntil(lambda: win.couch is not None and win.couch.isVisible(), timeout=5000)
    couch = win.couch
    gl = GL(couch)
    qtbot.waitUntil(lambda: couch.intro is not None and couch.intro.state == "playing", timeout=3000)
    qtbot.wait(700)                                              # the logo is on screen by now
    img = gl.draw(couch)
    assert _lit(img) > 0.002                                     # not just the dark background
    couch.force_close()


def test_gpu_setting_chooses_the_window_and_falls_back_when_opengl_does_not_start(ui, qtbot, monkeypatch):
    win, core, _ = ui
    from ezgd.ui import couch as C
    win.actions.settings["gpu_acceleration"] = True
    monkeypatch.setenv("EZGD_COUCH_GPU", "0")                   # no OpenGL: the CPU window, as before
    couch = _open_couch(win, qtbot)
    assert type(couch) is C.CouchWindow and not couch.gpu_paint
    qtbot.wait(300)
    cpu = couch.grab().toImage()
    couch.force_close()
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)

    # with OpenGL: the window drawn by the GPU (offscreen it draws into its framebuffer, it is only never shown)
    monkeypatch.setenv("EZGD_COUCH_GPU", "1")
    monkeypatch.setattr(C.CouchGLWindow, "GL_CHECK_MS", 300)
    if not C.gpu_available():
        pytest.skip("no OpenGL on this computer")
    couch = _open_couch(win, qtbot)
    assert type(couch) is C.CouchGLWindow and couch.gpu_paint
    qtbot.waitUntil(lambda: couch._gl_frames > 0, timeout=5000)
    qtbot.wait(500)                                             # (its own check passed: still the same window)
    assert win.couch is couch and not couch._paint_failed
    g = couch.grab().toImage()
    assert _diff(cpu, g) < 2.0
    for _ in range(3):                                          # partial frames (the focus moves) …
        couch.command("right")
        qtbot.wait(150)
    assert not couch._paint_failed
    couch._request(QRect(0, 0, 4, 4))                           # … left the same picture as a whole new frame
    kept = couch.grabFramebuffer()
    assert _diff(kept, couch.grab().toImage()) < 0.5

    # OpenGL stops working (a broken driver: no context): couch mode opens again, drawn by the CPU, and says why
    monkeypatch.setattr(C.CouchGLWindow, "isValid", lambda self: False)
    couch._gl_check()
    qtbot.waitUntil(lambda: win.couch is not None and type(win.couch) is C.CouchWindow, timeout=5000)
    assert win.couch.isVisible() and not win.isVisible()
    assert any(t["title"] == "Drawn by the CPU this time" for t in win.couch.toasts.items)
    assert win.actions.toasts is win.couch.toasts
    win.couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)
    couch = _open_couch(win, qtbot)                             # for the rest of this run: the CPU
    assert type(couch) is C.CouchWindow
    couch.force_close()

    win.actions.settings["gpu_acceleration"] = False            # switched off: never the OpenGL window
    qtbot.waitUntil(lambda: win.couch is None, timeout=5000)
    win._couch_gpu_failed = False
    couch = _open_couch(win, qtbot)
    assert type(couch) is C.CouchWindow
    assert not getattr(couch, "gpu", False)                     # … nor its frames through OpenGL: one switch
    couch.force_close()


def test_desktop_gpu_switches_at_start_up(tmp_path):
    import json
    import sqlite3
    db = tmp_path / "ezgd.sqlite3"
    data = tmp_path
    env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--foo"}
    assert gpu.apply_at_startup(data, db, env) == "off" and env == {"QTWEBENGINE_CHROMIUM_FLAGS": "--foo"}
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)")
    c.execute("INSERT INTO settings VALUES ('gpu_acceleration', ?, 0)", (json.dumps(False),))
    c.commit()
    assert gpu.apply_at_startup(data, db, env) == "off"
    c.execute("UPDATE settings SET value = ? WHERE key = 'gpu_acceleration'", (json.dumps(True),))
    c.commit()
    c.close()
    assert gpu.apply_at_startup(data, db, env) == "on"
    assert "QT_WIDGETS_RHI" not in env           # desktop windows stay as they are (menus drew garbled through it)
    flags = env["QTWEBENGINE_CHROMIUM_FLAGS"].split()
    assert flags[0] == "--foo" and "--enable-gpu-rasterization" in flags and "--ignore-gpu-blocklist" in flags
    assert (data / gpu.MARKER).exists()
    # that start never got as far as its window: the next one leaves the GPU out (and the one after tries again)
    env2 = {}
    assert gpu.apply_at_startup(data, db, env2) == "skipped" and env2 == {}
    assert gpu.apply_at_startup(data, db, env2) == "on"
    gpu.started(data)
    assert not (data / gpu.MARKER).exists()
    assert gpu.apply_at_startup(data, db, {}) == "on"            # a good start: on again next time


def test_gpu_partial_frames_leave_no_trails(ui, qtbot, monkeypatch):
    """Couch mode's Settings flickered when drawn by the GPU: a list set its own clip, replacing the frame's, so
    every partial frame painted the list again over the last one. What stays on screen must equal a whole frame."""
    from PySide6.QtCore import QRect
    from ezgd.ui import couch as C
    win, core, _ = ui
    monkeypatch.setenv("EZGD_COUCH_GPU", "1")
    win.actions.settings["gpu_acceleration"] = True
    if not C.gpu_available():
        pytest.skip("no OpenGL on this computer")
    couch = _open_couch(win, qtbot)
    assert type(couch) is C.CouchGLWindow
    couch._switch_tab(C.T_SETTINGS, force=True)
    qtbot.wait(300)
    couch.zone = "main"
    for _ in range(6):
        couch.command("down")
        qtbot.wait(450)
        couch._request(QRect(0, 0, 2, 2))
        kept = couch.grabFramebuffer()                       # what the partial frames left on screen
        assert _diff(kept, couch.grab().toImage()) < 0.5     # … the same as a whole new frame
    couch.force_close()
