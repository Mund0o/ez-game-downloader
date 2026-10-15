"""Couch mode under thousands of random controller presses: every page, overlay, keyboard and the quick menu — no
error, no frame that fails to draw, no selection out of range. (FUZZ_SEEDS / FUZZ_STEPS / FUZZ_GPU / FUZZ_SCALE
run longer or other variants by hand.)"""

import logging
import os
import random
import time
from pathlib import Path

import pytest
from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401

CMDS = ["up"] * 6 + ["down"] * 6 + ["left"] * 5 + ["right"] * 5 + ["a"] * 6 + ["b"] * 5 + ["x"] * 2 + \
    ["lb", "rb", "lt", "rt", "start", "select", "home"]


def _games(win, core, tmp_path):
    root = tmp_path / "games"
    made = []
    for k, name in enumerate(("Fuzz One", "Fuzz Two", "Fuzz Three")):
        folder = root / name
        folder.mkdir(parents=True)
        exe = folder / "game.sh"
        exe.write_text("#!/bin/sh\nsleep 300\n")
        exe.chmod(0o755)
        (folder / "Game.exe").write_bytes(b"MZ" + b"\0" * 100)
        e = win.bridge.run_sync(core.library_call("import_folder", name, str(folder)))
        if k == 0:
            win.bridge.run_sync(core.library_call("set_executable", e.id, str(exe)))
            win.bridge.run_sync(core.library_call("mark_installed", e.id, str(folder), str(exe)))
        made.append(e)
    win._reload_library()
    return made


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.mark.parametrize("seed", [int(s) for s in os.environ.get("FUZZ_SEEDS", "1,2").split(",")])
def test_couch_mode_survives_random_presses(ui, qtbot, tmp_path, monkeypatch, seed):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home-dir"))
    (tmp_path / "home-dir").mkdir()
    win, core, _ = ui
    games = _games(win, core, tmp_path)
    if os.environ.get("FUZZ_GPU") == "1":
        monkeypatch.setenv("EZGD_COUCH_GPU", "1")
        win.actions.settings["gpu_acceleration"] = True
    if os.environ.get("FUZZ_SCALE"):
        win.actions.settings["couch_ui_scale"] = int(os.environ["FUZZ_SCALE"])
    couch = _open_couch(win, qtbot)
    couch.actions.settings["couch_controller_only"] = False
    rnd = random.Random(seed)
    steps = int(os.environ.get("FUZZ_STEPS", "1200"))
    errors = _Errors()
    logging.getLogger("ezgd.ui").addHandler(errors)
    monkeypatch.setattr(errors, "records", errors.records)
    trail = []
    seen = set()
    import sys
    hooked = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: hooked.append((t, v, tb)))

    def invariants(c) -> str:
        from ezgd.ui.couch import X_INDEX
        if c.zone not in ("top", "buttons", "main"):
            return f"zone {c.zone}"
        if not 0 <= c.top_index <= X_INDEX:
            return f"top_index {c.top_index}"
        o = c.overlay
        if o is not None and o.buttons and not o.busy and not 0 <= o.focus < len(o.buttons):
            return f"overlay focus {o.focus}/{len(o.buttons)} in {o.title}"
        pg = c.current_page()
        items = getattr(pg, "items", None)
        if pg is not None and items and hasattr(pg, "focus") and not 0 <= pg.focus < len(items):
            return f"{type(pg).__name__} focus {pg.focus}/{len(items)}"
        if pg is None and c.tab in (0, 1):
            n = len(c._items(c.tab))
            if n and not 0 <= c.index[c.tab] < n:
                return f"index {c.index[c.tab]}/{n} on tab {c.tab}"
        q = c.quick
        if q is not None and q.isVisible() and q.view == "main" and q.rows and not 0 <= q.focus < len(q.rows):
            return f"quick focus {q.focus}/{len(q.rows)}"
        return ""
    from PySide6.QtWidgets import QApplication
    t0 = time.monotonic()
    for i in range(steps):
        if win.couch is None:
            win.open_couch()
            qtbot.waitUntil(lambda: win.couch is not None and win.couch.isVisible(), timeout=5000)
            couch = win.couch
            couch.actions.settings["couch_controller_only"] = False
        r = rnd.random()
        if couch.keyboard is not None and r < 0.3:
            ch = rnd.choice("abc12 ")
            couch.keyboard.type(ch)
            trail.append(f"type {ch}")
        elif r < 0.02 and couch.running:
            couch._home_held()
            trail.append("hold")
        elif couch.quick is not None and couch.quick.isVisible():
            name = rnd.choice(CMDS)
            couch._pad(name)
            trail.append(f"quick {name}")
        else:
            name = rnd.choice(CMDS)
            couch.command(name)
            trail.append(name)
        QApplication.processEvents()
        c = win.couch
        if c is not None:
            seen.add(("tab", c.tab))
            pg = c.current_page()
            seen.add(("page", type(pg).__name__ if pg else None))
            if c.overlay is not None:
                seen.add(("overlay", c.overlay.tag or c.overlay.title[:24]))
            if c.keyboard is not None:
                seen.add(("keyboard", c.keyboard.title[:20]))
            if c.quick is not None and c.quick.isVisible():
                seen.add(("quick", c.quick.view))
        if i % 10 == 0:
            qtbot.wait(25)
            if win.couch is not None and win.couch.isVisible():
                win.couch.grab()
                assert not win.couch._paint_failed, trail[-30:]
        if hooked:
            import traceback
            t, v, tb = hooked[0]
            raise AssertionError(f"step {i}: unhandled {''.join(traceback.format_exception(t, v, tb))}\n"
                                 f"trail: {trail[-40:]}")
        if win.couch is not None:
            why = invariants(win.couch)
            assert not why, f"step {i}: {why}\ntrail: {trail[-40:]}"
        bad = errors.records
        if bad:
            raise AssertionError(f"step {i}: {bad[0].getMessage()}\n{bad[0].exc_text or ''}\ntrail: {trail[-40:]}")
    print(f"seed {seed}: {steps} steps in {time.monotonic() - t0:.1f}s", flush=True)
    for k in sorted({x[0] for x in seen}):
        print("  ", k, sorted(str(x[1]) for x in seen if x[0] == k), flush=True)
    for e in games:
        try:
            win.bridge.run_sync(core.library_call("stop_completely", e.id, 2))
        except Exception:
            pass
    if win.couch is not None:
        win.couch.force_close()
    logging.getLogger("ezgd.ui").removeHandler(errors)
