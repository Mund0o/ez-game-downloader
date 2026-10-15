"""Couch mode can do what the desktop does: finish setup step by step, every fix and setting of a game, its
findings, collections, rename, library-wide checks — and stop a running Fix my game test."""

from test_couch_ui import _open_couch
from test_ui import ui  # noqa: F401 — the shared main-window fixture


def _extracted(win, core, tmp_path, name="Setup Game"):
    folder = tmp_path / "games" / name
    folder.mkdir(parents=True)
    (folder / "Game.exe").write_bytes(b"MZ" + b"\0" * 4000)
    e = win.bridge.run_sync(core.library_call("import_folder", name, str(folder)))
    win._reload_library()
    return e


def test_finish_setup_walks_to_play(ui, qtbot, tmp_path):
    from ezgd.ui.couch_pages import GameSetupPage
    win, core, _ = ui
    e = _extracted(win, core, tmp_path)
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: any(x.id == e.id for x in couch.entries), timeout=5000)
    entry = next(x for x in couch.entries if x.id == e.id)
    assert not couch.ready(entry)
    couch.primary(entry)                                         # A on a game that is not ready: the checklist
    page = couch.current_page()
    assert isinstance(page, GameSetupPage)
    steps, nxt = page.steps()
    assert [st[0] for st in steps][-2:] == ["Choose the game's program", "Play"] and nxt is not None
    couch.grab()
    assert not couch._paint_failed
    page.focus = 0
    couch.command("a")                                           # the next step: the app picks Game.exe itself
    qtbot.waitUntil(lambda: core.library.get(e.id).executable, timeout=5000)
    win._reload_library()
    qtbot.waitUntil(lambda: couch.ready(next(x for x in couch.entries if x.id == e.id)), timeout=5000)
    steps, nxt = page.steps()
    assert steps[-1][1] == "now" and nxt[0].endswith("Play")
    couch.grab()
    couch.force_close()


def test_manage_game_settings_and_library_tools(ui, qtbot, tmp_path):
    from ezgd.ui import couch as C
    win, core, _ = ui
    e = _extracted(win, core, tmp_path, "Tool Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: any(x.id == e.id for x in couch.entries), timeout=5000)
    entry = next(x for x in couch.entries if x.id == e.id)
    couch.manage(entry)
    labels = [b[0] for b in couch.overlay.buttons]
    for want in ("Finish setup", "Game settings", "Collections…", "Rename…", "Remove from the Library…"):
        assert want in labels
    couch.grab()
    couch._close_overlay()
    couch.open_game_settings(entry)
    page = couch.current_page()
    qtbot.waitUntil(lambda: page.loaded, timeout=5000)
    names = [it.label for it in page.items]
    assert "Run with" in names and "Steam App ID" in names and any(n.startswith("Fixes — ") for n in names)
    couch.grab()
    assert not couch._paint_failed
    couch.pop_page()
    couch._collections(entry)                                    # a new collection, from the on-screen keyboard
    qtbot.waitUntil(lambda: couch.overlay is not None and couch.overlay.tag == "menu", timeout=5000)
    couch._new_collection(entry)
    couch.keyboard.text = "Couch faves"
    couch.keyboard.done()
    qtbot.waitUntil(lambda: core.library.get(e.id).collections == ["Couch faves"], timeout=5000)
    couch._close_overlay()
    # library-wide: import a game folder that is not in the Library yet
    (tmp_path / "games" / "Loose Game").mkdir(parents=True)
    couch._switch_tab(C.T_SETTINGS, force=True)
    sp = couch.current_page()
    sp._build()
    assert {"Check every game for updates", "Storage", "Clean up leftovers", "Import a game"} <= \
        {it.label for it in sp.items}
    sp._import()
    assert couch.overlay is not None and any(b[0] == "Loose Game" for b in couch.overlay.buttons)
    couch.force_close()


def test_fix_my_game_testing_can_be_stopped(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    e = _extracted(win, core, tmp_path, "Test Game")
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: any(x.id == e.id for x in couch.entries), timeout=5000)
    entry = next(x for x in couch.entries if x.id == e.id)
    stopped = []

    async def cancel():
        stopped.append(1)
    monkeypatch.setattr(core, "cancel_trials", cancel)
    from ezgd.ui.couch import Overlay
    couch.overlay = Overlay("Fixing…", "", busy=True, tag=f"fixing:{e.id}", progress=0.3,
                            cancel=lambda: couch._stop_fix(entry), buttons=[("■  Stop testing",
                                                                             lambda: couch._stop_fix(entry), "ghost")])
    couch.grab()
    couch.command("b")                                           # B (or A on "Stop testing") ends the test
    qtbot.waitUntil(lambda: stopped == [1], timeout=5000)
    assert couch.overlay.text.startswith("Stopping the test")
    couch.overlay = None
    couch.force_close()
