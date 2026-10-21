"""Hidden test starts and what their pictures show (the real KWin is never touched: EZGD_HIDDEN=0, faked here)."""

import shutil
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from ezgd.library import picture


def frame(text: str = "", shade: int = 0, spot: int | None = None) -> QImage:
    img = QImage(1280, 720, QImage.Format.Format_RGB32)
    img.fill(QColor(shade, shade, shade))
    p = QPainter(img)
    if spot is not None:                                  # something moving: a bright block at another place
        p.fillRect(spot, 300, 200, 120, QColor(220, 200, 160))
    if text:
        p.setPen(QColor(150, 150, 150))                   # dim text on black, like Expedition 33's title
        f = QFont("Sans")
        f.setPixelSize(48)
        p.setFont(f)
        p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return img


def test_black_frozen_and_moving_pictures(qapp):
    black = [picture.analyze(frame(), at, read_text=False) for at in (8, 24, 40)]
    assert picture.judge(black)[0] == "black"
    same = [picture.analyze(frame(shade=40, spot=100), at, read_text=False) for at in (8, 24)]
    assert picture.judge(same)[0] == "frozen"
    moving = [picture.analyze(frame(shade=40, spot=x), at, read_text=False) for at, x in ((8, 100), (24, 700))]
    assert picture.judge(moving)[0] == "moving"


@pytest.mark.skipif(not shutil.which("tesseract"), reason="needs tesseract")
def test_a_menu_and_an_error_drawn_by_the_game(qapp):
    menu = picture.analyze(frame("PRESS ANY BUTTON"), 35)
    assert menu.menu and picture.judge([menu])[0] == "menu"
    err = picture.analyze(frame("Fatal error: failed to create the D3D12 device"), 12)
    state, why = picture.judge([err])
    assert state == "error" and "failed" in why.lower()


def test_a_hidden_test_is_judged_by_its_picture(library, tmp_path, monkeypatch, qapp):  # noqa: F811
    """The test game runs off-screen; its window's pictures say it reached its menu — not 'no sign it got past
    start-up'. Its windows are checked against the test's own processes and its sound is muted."""
    from ezgd.platform import hidden
    folder = tmp_path / "games" / "H"
    folder.mkdir(parents=True)
    script = folder / "run.sh"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    e = library.import_folder("H", folder)
    library.set_executable(e.id, script)
    calls = {"start": 0, "stop": 0, "mute": 0, "check": 0}

    class FakeSession:
        def __init__(self, names=None):
            self.names = names

        def start(self):
            calls["start"] += 1
            return True

        def check(self, ours):
            calls["check"] += 1
            assert ours                                   # the test's own processes are known
            return [{"pid": min(ours), "id": "{w}", "class": "steam_app_480", "away": True}]

        def mute(self, pids):
            calls["mute"] += 1

        def stop(self):
            calls["stop"] += 1

    shots = iter([frame(), frame("PRESS ANY BUTTON", spot=200), frame("PRESS ANY BUTTON", spot=500)])
    monkeypatch.setattr(hidden, "available", lambda: True)
    monkeypatch.setattr(hidden, "HiddenSession", FakeSession)
    monkeypatch.setattr(hidden, "capture", lambda wid: next(shots))
    monkeypatch.setattr(library, "SCREEN_LOOKS", (1, 2, 3))
    monkeypatch.setattr(library, "TRIAL_SECONDS", 5)
    started = time.monotonic()
    r = library.run_trial(e.id, {"label": "hidden", "overrides": {}}, 1)
    assert time.monotonic() - started < 30
    assert calls["start"] == 1 and calls["stop"] == 1 and calls["check"] >= 2 and calls["mute"] >= 2
    if shutil.which("tesseract"):
        assert r["outcome"] == "ran" and r["picture"] == "menu" and not r["stuck"], r
        assert "showed its menu" in r["detail"]
    assert r.get("picture_file")


@pytest.fixture
def library(db, settings, app_paths):
    from ezgd.database.repositories import GameRepository, LibraryRepository
    from ezgd.library.manager import LibraryManager
    return LibraryManager(LibraryRepository(db), GameRepository(db), settings, app_paths, lambda n, p: None)
