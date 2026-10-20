"""Couch mode's intro video, and the couch drawing optimisations (text wrapping, cached layers)."""

import time

import pytest
from PySide6.QtGui import QFont

from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.ui import couch_intro


@pytest.fixture
def intro_on(ui, monkeypatch):          # noqa: F811
    win, core, _ = ui
    monkeypatch.delenv("EZGD_NO_INTRO", raising=False)
    win.bridge.run_sync(core.set_setting("couch_intro_sound", False))      # never through the real speakers
    win.actions.settings["couch_intro_sound"] = False
    win.actions.settings["couch_intro"] = True
    return ui


def _open(win, qtbot):
    win.open_couch()
    qtbot.waitUntil(lambda: win.couch is not None and win.couch.isVisible(), timeout=5000)
    return win.couch


@pytest.mark.skipif(not couch_intro.VIDEO.is_file(), reason="intro video not built")
def test_intro_plays_then_couch_mode_and_its_welcome(intro_on, qtbot):
    win, _core, _ = intro_on
    couch = _open(win, qtbot)
    assert couch.intro is not None and couch.intro.active
    assert not any(t["title"] == "Couch mode" for t in couch.toasts.items)     # the welcome waits for the intro
    qtbot.waitUntil(lambda: couch.intro is not None and couch.intro.state == "playing", timeout=3000)
    couch.grab()
    assert not couch._paint_failed
    qtbot.waitUntil(lambda: couch.intro is None, timeout=6000)                 # ends by itself, fades out
    assert any(t["title"] == "Couch mode" for t in couch.toasts.items)
    couch.grab()
    assert not couch._paint_failed
    couch.force_close()


@pytest.mark.skipif(not couch_intro.VIDEO.is_file(), reason="intro video not built")
def test_any_button_skips_the_intro_and_does_nothing_else(intro_on, qtbot):
    win, _core, _ = intro_on
    couch = _open(win, qtbot)
    qtbot.waitUntil(lambda: couch.intro is not None and couch.intro.state == "playing", timeout=3000)
    zone, tab = couch.zone, couch.tab
    t0 = time.monotonic()
    couch.command("rb")                         # would switch tabs — here it only skips the intro
    assert couch.tab == tab and couch.zone == zone
    qtbot.waitUntil(lambda: couch.intro is None, timeout=2000)
    assert time.monotonic() - t0 < 1.5
    couch.command("rb")                         # now the controller drives couch mode again
    assert couch.tab != tab
    couch.force_close()


@pytest.mark.skipif(not couch_intro.VIDEO.is_file(), reason="intro video not built")
def test_leaving_during_the_intro_stops_it(intro_on, qtbot):
    win, _core, _ = intro_on
    couch = _open(win, qtbot)
    intro = couch.intro
    assert intro is not None
    with qtbot.waitSignal(couch.closed, timeout=5000):
        couch.request_exit()                    # the ✕ flow (no game running: closes at once)
    assert intro.state == "done" and intro._player is None
    qtbot.waitUntil(lambda: win.isVisible(), timeout=3000)


def test_intro_switched_off_or_missing_opens_couch_mode_directly(intro_on, qtbot, monkeypatch, tmp_path):
    win, _core, _ = intro_on
    win.actions.settings["couch_intro"] = False
    couch = _open(win, qtbot)
    assert couch.intro is None
    assert any(t["title"] == "Couch mode" for t in couch.toasts.items)
    with qtbot.waitSignal(couch.closed, timeout=5000):
        couch.request_exit()
    qtbot.waitUntil(lambda: win.couch is None, timeout=3000)
    win.actions.settings["couch_intro"] = True
    monkeypatch.setattr(couch_intro, "VIDEO", tmp_path / "missing.webm")
    couch = _open(win, qtbot)
    assert couch.intro is None
    couch.force_close()


def test_a_broken_video_is_skipped(intro_on, qtbot, monkeypatch, tmp_path):
    win, _core, _ = intro_on
    bad = tmp_path / "intro.webm"
    bad.write_bytes(b"not a video" * 100)
    monkeypatch.setattr(couch_intro, "VIDEO", bad)
    couch = _open(win, qtbot)
    qtbot.waitUntil(lambda: couch.intro is None, timeout=couch_intro.FIRST_FRAME_WAIT * 1000 + 3000)
    couch.grab()
    assert not couch._paint_failed
    couch.force_close()


def test_wrap_lines_is_fast_and_ends_with_an_ellipsis(qapp):
    from ezgd.ui.couch_pages import wrap_lines
    f = QFont()
    f.setPixelSize(18)
    text = " ".join(["word"] * 20000)                # a very long description
    t0 = time.perf_counter()
    lines = wrap_lines(text, f, 600, 8)
    assert time.perf_counter() - t0 < 0.1
    assert len(lines) == 8 and lines[-1].endswith("…")
    assert wrap_lines("short text", f, 600, 8) == ["short text"]


def test_layer_cache_keeps_to_its_byte_budget(ui, qtbot):  # noqa: F811
    win, _core, _ = ui
    couch = _open(win, qtbot)
    couch.LAYER_BYTES = 8 << 20
    for k in range(40):
        couch._layer(("test", k), 800, 400, lambda p: None)
    assert couch._layer_bytes <= couch.LAYER_BYTES
    assert couch._layer_bytes == sum(v[0].sizeInBytes() for v in couch._layers.values())
    couch.drop_layers("test")
    assert not any(k[0][0] == "test" for k in couch._layers)
    couch.force_close()


class _FakeBrowserDownloads:
    def __init__(self):
        self.calls = []
        self.items = [{"id": 1, "name": "Game.part1.rar", "game": "Browser Game", "host": "buzzheavier.com",
                       "state": "downloading", "received": 50 << 20, "total": 200 << 20, "speed": 4 << 20,
                       "eta": 37.0, "error": "", "library": "", "path": "/tmp/x"},
                      {"id": 2, "name": "Done.zip", "game": "Finished Game", "host": "gofile.io", "state": "completed",
                       "received": 10, "total": 10, "speed": 0, "eta": None, "error": "", "library": "added",
                       "path": "/tmp/y"}]

    def snapshots(self):
        return [dict(d) for d in self.items]

    def pause(self, did):
        self.calls.append(("pause", did))

    def resume(self, did):
        self.calls.append(("resume", did))

    def cancel(self, did):
        self.calls.append(("cancel", did))

    def remove(self, did):
        self.calls.append(("remove", did))


def test_in_app_browser_downloads_show_in_couch_downloads(ui, qtbot, monkeypatch):  # noqa: F811
    win, _core, _ = ui
    couch = _open(win, qtbot)
    fake = _FakeBrowserDownloads()
    monkeypatch.setattr(couch, "_browser_manager", lambda start=True: fake)
    from ezgd.ui.couch import T_DOWNLOADS
    couch._switch_tab(T_DOWNLOADS, force=True)
    couch._poll_browser()
    rows = couch._download_rows()
    assert [k for k, _d in rows] == ["browser", "browser"]                 # running first, finished last
    assert rows[0][1]["game"] == "Browser Game"
    assert couch._top_status().startswith("↓")                             # the speed shows in the top bar
    couch.grab()
    assert not couch._paint_failed
    couch.zone = "main"
    couch.index[T_DOWNLOADS] = 0
    couch.command("a")                                                      # the download's menu
    assert couch.overlay is not None and couch.overlay.title == "Browser Game"
    pause = next(r for r in couch.overlay.buttons if r[0] == "Pause")
    pause[1]()
    assert fake.calls == [("pause", 1)]
    couch.force_close()


def test_browser_drops_a_second_request_for_the_same_file(qapp):
    """A double click (or a page asking twice) must not download the same file twice."""
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not available")

    class Url:
        def __init__(self, u):
            self.u = u

        def toString(self):
            return self.u

    class Req:
        def __init__(self, name, url):
            self.name, self._url, self.state = name, Url(url), "new"

        def downloadFileName(self):
            return self.name

        def url(self):
            return self._url

        def cancel(self):
            self.state = "cancelled"

    class Running:
        final = None

        def __init__(self, req):
            self.req = req

    class Fake:
        _session_for = staticmethod(lambda page: object())
        items = {1: Running(Req("Game.part1.rar", "https://host/a?token=1"))}
    Req.page = lambda self: None
    for name, url in (("Game.part1 (1).rar", "https://host/a?token=2"), ("Other.rar", "https://host/a?token=1")):
        r = Req(name, url)
        browser.BrowserDownloads._requested(Fake(), r)
        assert r.state == "cancelled"
