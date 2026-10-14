"""Couch mode (full screen, controller) and the new pages: Wishlist, Storage, Library updates/saves, app updates."""

import json
import sys

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from test_ui import ui  # noqa: F401 — the shared main-window fixture

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX script as the game")


def _buttons(widget, text):
    return [b for b in widget.findChildren(QPushButton) if b.text().strip() == text and b.isVisible()]


def _game(win, core, tmp_path, name="Couch Game", seconds=300, exe_name="game.sh"):
    folder = tmp_path / "games" / name
    folder.mkdir(parents=True)
    exe = folder / exe_name
    exe.write_text(f"#!/bin/sh\nsleep {seconds}\n")
    exe.chmod(0o755)
    e = win.bridge.run_sync(core.library_call("import_folder", name, str(folder)))
    win.bridge.run_sync(core.library_call("set_executable", e.id, str(exe)))
    win.bridge.run_sync(core.library_call("mark_installed", e.id, str(folder), str(exe)))
    return core.library.get(e.id)


def _open_couch(win, qtbot):
    win._reload_library()
    qtbot.waitUntil(lambda: bool(win.pages["library"].entries) or True, timeout=3000)
    win.open_couch()
    qtbot.waitUntil(lambda: win.couch is not None and win.couch.isVisible(), timeout=5000)
    return win.couch


def test_couch_opens_full_screen_and_the_x_brings_the_window_back(ui, qtbot, tmp_path):
    win, core, _ = ui
    _game(win, core, tmp_path)
    win._reload_library()
    qtbot.waitUntil(lambda: len(win.pages["library"].entries) == 1, timeout=5000)
    couch = _open_couch(win, qtbot)
    assert couch.isFullScreen() and not win.isVisible()
    assert win.actions.toasts is couch.toasts                  # messages show inside couch mode
    assert not QApplication.instance().quitOnLastWindowClosed()
    qtbot.waitUntil(lambda: len(couch.entries) == 1, timeout=5000)
    for tab in (0, 1, 2):                                      # every page draws without errors, at any size
        couch._switch_tab(tab)
        for w, h in ((1920, 1080), (1280, 720), (3840, 2160), (800, 500)):
            couch.resize(w, h)
            couch.grab()
            assert not couch._paint_failed
    couch._switch_tab(0)
    # controller: up → the hero buttons, up → the top bar, right ×5 → the ✕, A → leave
    for name in ("up", "up", "right", "right", "right", "right", "right"):
        couch.command(name)
    assert couch.zone == "top" and couch.top_index == 3
    with qtbot.waitSignal(couch.closed, timeout=5000):
        couch.command("a")
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)
    assert win.couch is None and win.actions.toasts is win.toasts
    assert QApplication.instance().quitOnLastWindowClosed()


@posix_only
def test_couch_x_quits_the_game_it_started_then_closes(ui, qtbot, tmp_path):
    win, core, _ = ui
    e = _game(win, core, tmp_path)
    win._reload_library()
    qtbot.waitUntil(lambda: len(win.pages["library"].entries) == 1, timeout=5000)
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: len(couch.entries) == 1, timeout=5000)
    couch.command("a")                                         # A on the game: "Play …?" (confirm is on)
    qtbot.waitUntil(lambda: couch.overlay is not None and couch.overlay.title.startswith("Play"), timeout=5000)
    couch.grab()
    couch.command("a")                                         # ▶ PLAY
    qtbot.waitUntil(lambda: e.id in couch.running and e.id in couch.launched, timeout=10000)
    assert core.library.is_running(e.id)
    proc = core.library._processes[e.id][0]
    couch._minimize_timer.stop()
    # the window manager's close (Alt+F4) is the ✕ too: it asks first, nothing closes yet
    couch.close()
    assert couch.isVisible() and couch.overlay is not None and couch.overlay.tag == "exit"
    assert couch.overlay.focus == 1                            # "Cancel" is preselected
    couch.command("b")                                         # B = cancel: still playing
    assert couch.overlay is None and core.library.is_running(e.id)
    couch.request_exit()
    couch.command("left")
    couch.grab()
    with qtbot.waitSignal(couch.closed, timeout=30000):
        couch.command("a")                                     # "Quit game and leave"
    assert proc.poll() is not None                             # the game is really gone
    qtbot.waitUntil(lambda: not core.library.is_running(e.id), timeout=5000)
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


@posix_only
def test_couch_reports_a_game_that_would_not_close(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    e = _game(win, core, tmp_path)
    win._reload_library()
    qtbot.waitUntil(lambda: len(win.pages["library"].entries) == 1, timeout=5000)
    couch = _open_couch(win, qtbot)
    win.bridge.run_sync(core.library_call("launch", e.id, True))
    couch.launched.add(e.id)
    couch.running.add(e.id)
    monkeypatch.setattr(core.library, "stop_completely", lambda i, grace=10.0: {"stopped": False, "left": [4242]})
    couch.request_exit()
    couch.command("left")
    couch.command("a")
    qtbot.waitUntil(lambda: couch.overlay is not None and "could not be closed" in couch.overlay.title, timeout=10000)
    assert "4242" in couch.overlay.text and couch.isVisible()
    labels = [b[0] for b in couch.overlay.buttons]
    assert labels == ["Try again", "Leave anyway", "Cancel"]
    couch.grab()
    couch.command("right")
    with qtbot.waitSignal(couch.closed, timeout=5000):
        couch.command("a")                                     # Leave anyway
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)
    win.bridge.run_sync(core.library_call("stop", e.id))


def test_couch_input_is_ignored_while_a_game_is_played(ui, qtbot, tmp_path):
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch.running.add(999)
    couch.showMinimized()
    assert not couch._accepts_input()
    couch.showFullScreen()
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_wishlist_page_search_add_and_remove(ui, qtbot, routes):
    win, core, _ = ui
    routes.add("https://store.steampowered.com/api/storesearch/*", json.dumps({"items": [
        {"type": "app", "id": 4242, "name": "Silk Song", "tiny_image": None, "price": {"final": 1999, "currency": "USD"},
         "platforms": {"windows": True}}]}), content_type="application/json")
    routes.add("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/*", json.dumps({"response": {
        "store_items": [{"appid": 4242, "success": 1, "name": "Silk Song", "assets": {}, "release": {
            "is_coming_soon": True}}]}}), content_type="application/json")
    win.navigate("wishlist")
    page = win.pages["wishlist"]
    page.search.setText("silk")
    page._search_now()
    qtbot.waitUntil(lambda: bool(_buttons(page, "Add")), timeout=5000)
    _buttons(page, "Add")[0].click()
    qtbot.waitUntil(lambda: len(page.items) == 1, timeout=5000)
    assert page.items[0]["title"] == "Silk Song" and page.items[0]["coming_soon"]
    qtbot.waitUntil(lambda: bool(_buttons(page, "Remove")), timeout=5000)
    page.grab()
    _buttons(page, "Remove")[0].click()
    qtbot.waitUntil(lambda: page.items == [], timeout=5000)


@posix_only
def test_library_shows_update_saves_and_storage(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    e = _game(win, core, tmp_path, "Upd", exe_name="Upd.exe")       # a Windows game: it has a Proton prefix
    e.game_id = "onlinefix:1"
    e.release = {"version": "1.0", "latest": "1.1", "status": "update", "checked_at": 1}
    core.library.repo.update(e)
    from ezgd.catalog.models import Game
    core.games.save(Game("onlinefix", "1", "Upd", "https://online-fix.me/games/x/1-upd.html"))
    save = core.library.prefix_dir(e.id) / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "Upd"
    save.mkdir(parents=True)
    (save / "s.sav").write_text("s")
    win.navigate("library")
    page = win.pages["library"]
    qtbot.waitUntil(lambda: page.list.count() == 1, timeout=5000)
    page.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: bool(_buttons(page, "Update to 1.1")), timeout=5000)
    qtbot.waitUntil(lambda: bool(_buttons(page, "Back up now")), timeout=5000)
    _buttons(page, "Back up now")[0].click()
    qtbot.waitUntil(lambda: bool(_buttons(page, "Restore…")), timeout=5000)
    from ezgd.ui.dialogs.storage import StorageDialog
    dlg = StorageDialog(win.actions, page)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: bool(_buttons(dlg, "Uninstall…")), timeout=10000)
    dlg.grab()
    dlg.close()


def test_settings_app_updates_section(ui, qtbot, routes):
    win, core, _ = ui
    routes.add("https://api.github.com/repos/o/r/releases/latest", json.dumps({
        "tag_name": "v99.0.0", "assets": [], "html_url": "https://github.com/o/r/releases/v99.0.0"}),
        content_type="application/json")
    win.navigate("settings")
    page = win.pages["settings"]
    page.repo_edit.setText("https://github.com/o/r")
    page._save_repo()
    qtbot.waitUntil(lambda: "99.0.0" in page.app_update_info.text(), timeout=5000)
    assert "runs from source" in page.app_update_info.text()
    assert core.settings.settings.update_repo == "o/r"


@posix_only
def test_couch_cancel_at_play_then_x_closes_right_away(ui, qtbot, tmp_path):
    win, core, _ = ui
    _game(win, core, tmp_path)
    win._reload_library()
    qtbot.waitUntil(lambda: len(win.pages["library"].entries) == 1, timeout=5000)
    couch = _open_couch(win, qtbot)
    qtbot.waitUntil(lambda: len(couch.entries) == 1, timeout=5000)
    couch.command("a")
    qtbot.waitUntil(lambda: couch.overlay is not None and couch.overlay.title.startswith("Play"), timeout=5000)
    couch.command("b")                                         # cancel: nothing starts
    assert couch.overlay is None and not couch._starting_ids()
    with qtbot.waitSignal(couch.closed, timeout=5000):
        couch.request_exit()                                   # nothing running: no question


@posix_only
def test_couch_left_while_a_game_starts_closes_it_when_it_appears(ui, qtbot, tmp_path):
    win, core, _ = ui
    e = _game(win, core, tmp_path)
    win._reload_library()
    qtbot.waitUntil(lambda: len(win.pages["library"].entries) == 1, timeout=5000)
    couch = _open_couch(win, qtbot)
    import time
    couch._launch_at[e.id] = time.monotonic()                  # e.g. Steam is still starting it
    couch.request_exit()
    assert couch.overlay is not None and "still starting" in couch.overlay.text
    couch.command("left")
    with qtbot.waitSignal(couch.closed, timeout=5000):
        couch.command("a")                                     # Quit game and leave
    assert e.id in win._kill_on_start
    win.bridge.run_sync(core.library_call("launch", e.id, True))   # …and now the game appears
    qtbot.waitUntil(lambda: not core.library.is_running(e.id) and e.id not in win._kill_on_start, timeout=20000)
