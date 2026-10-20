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
    from ezgd.ui.couch import TABS, X_INDEX
    for tab in range(len(TABS)):                               # every page draws without errors, at any size
        couch._switch_tab(tab)
        for w, h in ((1920, 1080), (1280, 720), (3840, 2160), (800, 500)):
            couch.resize(w, h)
            couch.grab()
            assert not couch._paint_failed
    couch._switch_tab(0)
    # controller: up → the hero buttons, up → the top bar, right past every tab → the ✕, A → leave
    for name in ("up", "up") + ("right",) * (len(TABS) + 2):
        couch.command(name)
    assert couch.zone == "top" and couch.top_index == X_INDEX
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


def _until_overlay(qtbot, couch, starts, timeout=10000):
    def ready():
        assert couch.overlay is not None and not couch.overlay.busy, couch.overlay
        assert couch.overlay.title.startswith(starts), (couch.overlay.title, couch.overlay.text)
    qtbot.waitUntil(ready, timeout=timeout)
    return couch.overlay


def test_couch_store_search_game_page_download_and_downloads_menu(ui, qtbot):
    """Everything from the couch: search with the on-screen keyboard, open a game, download it, pause, cancel."""
    win, core, _ = ui
    from ezgd.catalog.models import Game
    from ezgd.ui.couch import T_DOWNLOADS, T_STORE
    couch = _open_couch(win, qtbot)
    couch.actions.settings["couch_controller_only"] = False    # the fixture games are not on (mocked) Steam
    couch.command("rb")
    couch.command("rb")                                        # Home → Library → Store
    assert couch.tab == T_STORE
    store = couch.pages[T_STORE]
    qtbot.waitUntil(lambda: bool(store.shelves), timeout=10000)
    for tab_size in ((1920, 1080), (1280, 720), (800, 500)):
        couch.resize(*tab_size)
        couch.grab()
        assert not couch._paint_failed
    couch.command("up")                                        # the filter chips
    assert store.row == 1
    couch.command("up")                                        # the search bar
    assert store.row == 0
    couch.command("a")                                         # opens the on-screen keyboard
    assert couch.keyboard is not None
    for ch in "dayz":
        couch.keyboard.type(ch)
    couch.command("b")                                         # B deletes a letter
    assert couch.keyboard.text == "day"
    couch.keyboard.type("z")
    couch.grab()
    couch.command("start")                                     # Start = Search
    assert couch.keyboard is None and store.query == "dayz" and store.filter == "search"
    qtbot.waitUntil(lambda: bool(store._grid_items()), timeout=10000)
    couch.grab()
    couch.command("a")                                         # open the first result
    assert couch.stack and couch.stack[-1].games
    couch.command("b")                                         # back to the results
    assert not couch.stack and store.query == "dayz"
    couch.command("b")                                         # B clears the search
    assert store.query == "" and store.filter == "discover"
    # filters: LT/RT flip through them; a site's grid keeps loading pages as you scroll
    couch.command("rt")
    assert store.filter == "all" and store.feed is not None
    qtbot.waitUntil(lambda: bool(store._grid_items()), timeout=10000)
    couch.grab()
    assert not couch._paint_failed
    couch.command("lt")
    assert store.filter == "discover"
    couch.command("up")                                        # the chips: Right + A picks one
    assert store.row == 1
    keys = [k for k, _l in store.chips()]
    while keys[store.chip] != "site:onlinefix":
        couch.command("right")
    couch.command("a")
    assert store.filter == "site:onlinefix" and store.row == 2
    qtbot.waitUntil(lambda: store.feed.pages.get("onlinefix", 0) >= 1 and bool(store._grid_items()), timeout=10000)
    couch.grab()
    couch.command("b")                                         # B: back to Discover
    assert store.filter == "discover"

    g = Game("onlinefix", "17801", "PEAK", "https://online-fix.me/games/adventures/17801-peak-po-seti.html")
    core.games.save(g)
    couch.open_game([g])
    page = couch.stack[-1]
    qtbot.waitUntil(lambda: any(it.label.startswith("Download — ") for it in page.items), timeout=10000)
    couch.grab()
    assert not couch._paint_failed
    assert page.items[page.focus].label == "Download — Torrent"    # a loaded page starts on its best download
    couch.command("a")
    ov = _until_overlay(qtbot, couch, "Download PEAK?")
    assert dict(ov.lines)["Password"].startswith("online-fix.me")
    couch.grab()
    couch.command("a")                                         # Download
    qtbot.waitUntil(lambda: couch.overlay is None, timeout=10000)
    snaps = win.bridge.run_sync(core.downloads_snapshot())
    assert [s["title"] for s in snaps] == ["PEAK"] and snaps[0]["kind"] == "torrent"
    # Downloads tab: A on the row → Pause, then Cancel (asks first)
    couch._switch_tab(T_DOWNLOADS)
    qtbot.waitUntil(lambda: bool(couch.downloads), timeout=5000)
    couch.grab()
    tid = snaps[0]["id"]
    couch.command("a")
    labels = [b[0] for b in couch.overlay.buttons]
    if "Pause" in labels:
        couch.overlay.focus = labels.index("Pause")
        couch.command("a")
        qtbot.waitUntil(lambda: win.bridge.run_sync(core.downloads_snapshot())[0]["status"] == "paused",
                        timeout=10000)
        qtbot.waitUntil(lambda: couch.overlay is None, timeout=5000)
        qtbot.waitUntil(lambda: couch.downloads and couch.downloads[0]["status"] == "paused", timeout=5000)
        couch.command("a")
        labels = [b[0] for b in couch.overlay.buttons]
    couch.overlay.focus = labels.index("Cancel download")
    couch.command("a")
    _until_overlay(qtbot, couch, "Cancel PEAK?")
    couch.command("left")
    couch.command("a")
    qtbot.waitUntil(lambda: all(s["status"] == "cancelled" or s["id"] != tid
                                for s in win.bridge.run_sync(core.downloads_snapshot())), timeout=10000)
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_settings_wishlist_and_manage(ui, qtbot, tmp_path, routes):
    win, core, _ = ui
    from ezgd.ui.couch import T_LIBRARY, T_SETTINGS, T_WISHLIST
    routes.add("https://store.steampowered.com/api/storesearch/*", json.dumps({"items": [
        {"type": "app", "id": 4242, "name": "Silk Song", "tiny_image": None, "price": {"final": 1999, "currency": "USD"},
         "platforms": {"windows": True}}]}), content_type="application/json")
    routes.add("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/*", json.dumps({"response": {
        "store_items": [{"appid": 4242, "success": 1, "name": "Silk Song", "assets": {}, "release": {
            "is_coming_soon": True}}]}}), content_type="application/json")
    e = _game(win, core, tmp_path)
    win._reload_library()
    qtbot.waitUntil(lambda: len(win.pages["library"].entries) == 1, timeout=5000)
    couch = _open_couch(win, qtbot)
    # Settings: A flips a switch for real
    couch._switch_tab(T_SETTINGS)
    page = couch.pages[T_SETTINGS]
    k = next(i for i, it in enumerate(page.items) if it.label.startswith("Ask before starting"))
    before = bool(core.settings.settings.confirm_before_launch)
    page.focus = k
    couch.command("a")
    qtbot.waitUntil(lambda: core.settings.settings.confirm_before_launch is (not before), timeout=5000)
    couch.command("a")
    qtbot.waitUntil(lambda: core.settings.settings.confirm_before_launch is before, timeout=5000)
    n = int(core.settings.settings.max_concurrent_downloads)
    page.focus = next(i for i, it in enumerate(page.items) if it.label == "Downloads at the same time")
    couch.command("right")
    qtbot.waitUntil(lambda: core.settings.settings.max_concurrent_downloads == min(16, n + 1), timeout=5000)
    couch.grab()
    # Wishlist: find a game on Steam with the keyboard and add it
    couch._switch_tab(T_WISHLIST)
    wl = couch.pages[T_WISHLIST]
    couch.command("a")                                         # "Find a game to add"
    assert couch.keyboard is not None
    for ch in "silk":
        couch.keyboard.type(ch)
    couch.command("start")
    _until_overlay(qtbot, couch, "Steam:")
    couch.command("a")                                         # the first match
    qtbot.waitUntil(lambda: len(wl.items) == 1, timeout=10000)
    assert wl.items[0]["title"] == "Silk Song"
    couch.grab()
    couch.command("down")
    couch.command("x")                                         # Y removes it
    qtbot.waitUntil(lambda: wl.items == [], timeout=10000)
    # Library: Y opens Manage with everything the desktop Library does
    couch._switch_tab(T_LIBRARY)
    couch.command("x")
    assert couch.overlay is not None and couch.overlay.menu and couch.overlay.title == e.title
    labels = [b[0] for b in couch.overlay.buttons]
    for want in ("▶  Play", "Choose the game file", "Fix problems", "Back up saves now", "Restore saves…",
                 "Uninstall…", "Close"):
        assert want in labels, want
    couch.grab()
    couch.overlay.focus = labels.index("Choose the game file")
    couch.command("a")
    _until_overlay(qtbot, couch, "Which program starts")
    assert any(b[0].startswith("game.sh") for b in couch.overlay.buttons)
    couch.command("b")
    couch.command("x")
    couch.overlay.focus = [b[0] for b in couch.overlay.buttons].index("Uninstall…")
    couch.command("a")
    _until_overlay(qtbot, couch, f"Uninstall {e.title}?")
    assert couch.overlay.focus == 1                            # "Cancel" is preselected
    couch.command("left")
    couch.command("a")
    qtbot.waitUntil(lambda: not win.bridge.run_sync(core.library_entries()), timeout=15000)
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_keyboard_and_x_while_typing(ui, qtbot):
    win, core, _ = ui
    from ezgd.ui.couch import T_STORE
    couch = _open_couch(win, qtbot)
    couch._switch_tab(T_STORE)
    couch.pages[T_STORE].open_keyboard()
    kb = couch.keyboard
    kb.row, kb.col = 1, 0
    couch.command("a")                                         # Q
    couch.command("down")
    couch.command("down")
    couch.command("down")                                      # to the bottom row
    assert kb.row == 4
    kb.col = 0
    couch.command("a")                                         # SPACE
    assert kb.text == "q "
    couch.grab()
    with qtbot.waitSignal(couch.closed, timeout=5000):         # the ✕ always works, even while typing
        couch.request_exit()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_store_tiles_name_every_site(ui, qtbot):
    win, core, _ = ui
    from ezgd.catalog.models import Game, GameGroup
    from ezgd.ui.couch_pages import source_tags
    games = [Game(src, "1", "Hades", f"https://{src}.example/hades") for src in
             ("steamrip", "crackedgames", "fitgirl", "ankergames", "onlinefix")]
    tags = source_tags(GameGroup("hades", games + [games[0]]))
    assert [t for t, _c in tags] == ["SteamRIP", "Cracked-Games", "FitGirl", "AnkerGames", "OnlineFix"]
    couch = _open_couch(win, qtbot)
    rows = couch._tag_rows(tags)
    assert [t for t, _c in rows] == ["SteamRIP", "Cracked-Games", "FitGirl", "+2 more"]
    assert [t for t, _c in couch._tag_rows(tags[:2])] == ["SteamRIP", "Cracked-Games"]
    pm = couch._render_tags(tags, 240, 1.0)
    assert not pm.isNull() and pm.height() > 0
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_fills_the_screen_and_keeps_a_steady_frame_rate(ui, qtbot):
    """A friend's couch mode made his VRR screen flicker (uneven frames move a VRR screen's refresh rate around),
    and a window that is no longer full screen asks for full screen again."""
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch.showNormal()
    couch.resize(900, 600)                                     # e.g. taken out of full screen by the desktop
    assert not couch.isFullScreen()
    couch._fit_screen()
    assert couch.isFullScreen()
    assert couch._pump.isActive() and 8 <= couch._pump.interval() <= 34    # one steady rate (≤ 75 Hz)
    couch._dirty = False
    couch.update()
    assert couch._dirty                                        # repaints wait for the next steady frame
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_holds_the_screen_while_a_game_starts(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    e = _game(win, core, tmp_path)
    win._reload_library()
    qtbot.waitUntil(lambda: len(win.pages["library"].entries) == 1, timeout=5000)
    couch = _open_couch(win, qtbot)
    calls = []
    monkeypatch.setattr(couch.wc, "keep_above", lambda on: calls.append(("above", on)))
    monkeypatch.setattr(couch.wc, "activate", lambda: calls.append(("activate",)))
    monkeypatch.setattr(couch, "isActiveWindow", lambda: False)
    couch._launching = True                                    # Play was pressed, the game is not up yet
    couch.launched.add(e.id)
    couch._take_focus_back()                                   # a Steam window took the focus: take it back
    assert ("activate",) in calls and ("above", True) in calls
    couch.running.add(e.id)                                    # the game is running and its window took the focus
    couch._launching = False
    couch._step_aside()
    assert couch.isMinimized() and ("above", False) in calls
    couch.running.discard(e.id)
    couch.launched.discard(e.id)
    calls.clear()
    couch._user_left()                                         # idle and the user picked another window
    assert calls == [("above", False)]
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_kwin_script_only_touches_this_apps_window(monkeypatch, tmp_path):
    import json
    import os
    from ezgd.ui import window_control as wcmod
    body = 'w.keepAbove = true;'
    script = wcmod.KWIN_SCRIPT % {"pid": os.getpid(), "marker": json.dumps('Couch "mode"'), "body": body}
    assert f"var pid = {os.getpid()}" in script and 'w.pid !== pid' in script
    assert '"Couch \\"mode\\""' in script and body in script


def test_couch_browser_is_driven_by_the_controller(ui, qtbot, server):
    """The in-app browser from couch mode: full screen, a pointer on the left stick, A clicks (real input
    events), the on-screen keyboard types into the page, the menu and B close it — no mouse or keyboard needed."""
    from ezgd.catalog.models import Game
    from ezgd.ui import couch_pages
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    server.add("/d/page", data=b'<html><body style="margin:0">'
                               b'<button id="b" style="position:absolute;left:0;top:0;width:100%;height:50%" '
                               b'onclick="document.title=\'clicked\'">Download</button>'
                               b'<input id="t" style="position:absolute;left:0;top:60%;width:100%;height:30%">'
                               b'</body></html>', content_type="text/html")
    g = Game("steamrip", "88", "Pad Game", "https://steamrip.com/pad-game/")
    core.games.save(g)
    couch = _open_couch(win, qtbot)
    couch_pages.open_browser(couch, g, None, server.url("/d/page"))
    cb = couch.browser
    assert cb is not None and cb.active and couch.pads.pointer_mode
    dlg = cb.dlg
    qtbot.waitUntil(lambda: dlg.isVisible(), timeout=5000)
    assert not dlg._bar_widget.isVisible() and dlg.zoom >= 1.0       # no desktop toolbar in couch mode
    loaded = []
    dlg.view.loadFinished.connect(loaded.append)
    qtbot.waitUntil(lambda: bool(loaded) or dlg.current.url().path() == "/d/page", timeout=20000)
    qtbot.wait(500)
    r = cb.view_rect()
    cb.pointer = r.center()
    cb.command("up")                                           # the D-pad nudges the pointer
    assert cb.pointer.y() < r.center().y()
    from PySide6.QtCore import QPointF
    cb.pointer = QPointF(r.center().x(), r.top() + r.height() * 0.2)    # over the button
    cb.button("a", True)
    assert couch.sounds.played[-1] == "click"                   # the pointer's mouse button is heard
    cb.button("a", False)
    qtbot.waitUntil(lambda: dlg.current.title() == "clicked", timeout=10000)
    cb.pointer = QPointF(r.center().x(), r.top() + r.height() * 0.75)   # the text box: the keyboard opens
    cb.button("a", True)
    cb.button("a", False)
    qtbot.waitUntil(lambda: cb.overlay.keyboard is not None, timeout=10000)
    for ch in "hades":
        cb.overlay.keyboard.type(ch)
    cb.overlay.grab()
    cb.command("start")                                        # Start on the keyboard = Enter: it types
    assert cb.overlay.keyboard is None and couch.sounds.played[-1] == "select"
    got = []
    dlg.current.runJavaScript("document.getElementById('t').value", 0, got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=5000)
    assert got[0] == "hades"
    cb.command("start")                                        # the menu
    assert cb.menu is not None and cb.menu[-1][0] == "Close the browser"
    n = len(couch.sounds.played)
    cb.command("down")
    assert couch.sounds.played[n:] == ["move"]
    cb.overlay.grab()
    cb.command("b")                                            # B closes the menu
    assert cb.menu is None
    cb.command("b")                                            # B on the first page: back to couch mode
    qtbot.waitUntil(lambda: couch.browser is None and not couch.pads.pointer_mode, timeout=5000)
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_browser_comes_back_to_couch_when_a_download_starts(ui, qtbot, server):
    import io
    import zipfile
    from PySide6.QtCore import QPointF
    from ezgd.catalog.models import Game
    from ezgd.ui import couch_pages
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("G/G.exe", b"MZ")
    server.add("/f/G.zip", data=buf.getvalue(), content_type="application/zip",
               headers={"Content-Disposition": 'attachment; filename="G.zip"'})
    server.add("/d/dl", data=b'<html><body style="margin:0"><a href="/f/G.zip" style="display:block;width:100%;'
                             b'height:100vh">Download</a></body></html>', content_type="text/html")
    g = Game("steamrip", "89", "Dl Game", "https://steamrip.com/dl-game/")
    core.games.save(g)
    couch = _open_couch(win, qtbot)
    couch_pages.open_browser(couch, g, None, server.url("/d/dl"))
    cb = couch.browser
    qtbot.waitUntil(lambda: cb.dlg.current.url().path() == "/d/dl", timeout=20000)
    qtbot.wait(500)
    r = cb.view_rect()
    cb.pointer = QPointF(r.center())
    cb.button("a", True)
    cb.button("a", False)
    qtbot.waitUntil(lambda: couch.browser is None, timeout=20000)      # the browser stepped aside by itself
    assert any(t["title"] == "Download started" for t in couch.toasts.items)
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_browser_stops_a_page_that_stays_frozen(ui, qtbot, server, monkeypatch):
    import time as _time
    from ezgd.catalog.models import Game
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    server.add("/d/x", data=b"<html><body>x</body></html>", content_type="text/html")
    g = Game("steamrip", "90", "Hang Game", "https://steamrip.com/hang-game/")
    core.games.save(g)
    dlg = browser.open_browser_download(win.actions, g, server.url("/d/x"), "TestHost", win)
    qtbot.waitUntil(lambda: dlg.current.url().path() == "/d/x", timeout=20000)
    old = dlg.current
    dlg._ping_page = old                                       # the page has not answered for a long time
    dlg._ping_sent = _time.monotonic() - browser.HANG_SECONDS - 1
    dlg._watchdog()
    assert dlg._hang_shown and dlg.current is old                # first: offered
    dlg._ping_sent = _time.monotonic() - browser.HANG_STOP_SECONDS - 1
    dlg._watchdog()
    assert dlg.current is not old                                # then: stopped by itself
    assert "stopped responding" in dlg.notice.text.text()
    dlg.close()


def test_store_all_games_from_every_site_with_sorting(ui, qtbot):
    from ezgd.catalog.models import Game
    from ezgd.ui.couch import T_STORE
    win, core, _ = ui
    for src, sid, title in (("onlinefix", "1", "Zeta Run"), ("fitgirl", "2", "Alpha Quest"),
                            ("steamrip", "3", "Mid Game"), ("fitgirl", "4", "Zeta Run")):
        core.games.save(Game(src, sid, title, f"https://{src}.example/{sid}"))
    couch = _open_couch(win, qtbot)
    couch.actions.settings["couch_controller_only"] = False
    couch._switch_tab(T_STORE)
    store = couch.pages[T_STORE]
    store.set_filter("all")
    qtbot.waitUntil(lambda: {"Zeta Run", "Alpha Quest", "Mid Game"} <= {g.title for g in store._grid_items()},
                    timeout=10000)
    zeta = next(g for g in store._grid_items() if g.title == "Zeta Run")
    assert {e.source for e in zeta.entries} >= {"onlinefix", "fitgirl"}          # one tile, both sites
    keys = [k for k, _l in store.chips()]
    assert keys[:3] == ["discover", "all", "new"] and keys[-1] == "sort"
    store.set_filter("sort")                                   # Sort ▾ → A – Z
    labels = [b[0] for b in couch.overlay.buttons]
    couch.overlay.focus = labels.index("A – Z")
    couch.command("a")
    titles = [g.title for g in store._grid_items()]
    assert titles == sorted(titles, key=str.casefold)
    couch.grab()
    assert not couch._paint_failed
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_never_overrules_the_full_screen_size(ui, qtbot, monkeypatch):
    """A friend's couch mode filled the screen for a moment, then shrank: it set the size Qt reported for the
    screen, which can be wrong with fractional scaling. Full screen's size is the window manager's alone."""
    from PySide6.QtCore import QRect
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    qtbot.wait(400)                                            # placed (and checked once) after opening
    assert couch.isFullScreen()
    given = couch.geometry()                                   # what the window manager gave it
    screen = couch.screen()

    class Wrong:                                               # Qt's screen, with a wrong size
        def geometry(self):
            return QRect(0, 0, 640, 480)

        def name(self):
            return screen.name()
    wrong = Wrong()
    monkeypatch.setattr(couch, "screen", lambda: wrong)
    monkeypatch.setattr(couch, "display_target", lambda: (wrong, QRect(0, 0, 640, 480), "fullscreen"))
    couch._fit_screen()                                        # Qt "thinks" the screen is 640×480
    couch._come_back()
    qtbot.wait(300)
    assert couch.isFullScreen() and couch.geometry() == given
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_custom_size_uses_the_real_scale(ui, qtbot, monkeypatch):
    """A custom resolution is in real pixels: at 150 % (where Qt's screen may claim 2×) 1200×600 real pixels are
    800×400 of the window's own pixels."""
    from ezgd.ui import couch as couch_module
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    monkeypatch.setattr(couch_module, "real_scale", lambda screen, first=None: 1.5)
    couch.actions.settings["couch_window"] = "custom"
    couch.actions.settings["couch_resolution"] = "1200x600"
    try:
        _screen, geo, mode = couch.display_target()
        assert mode == "custom" and (geo.width(), geo.height()) == (800, 400)
        couch.actions.settings["couch_resolution"] = "7680x4320"
        _screen, geo, _mode = couch.display_target()        # never bigger than the screen
        assert (geo.width(), geo.height()) == (couch.screen().geometry().width(), couch.screen().geometry().height())
    finally:
        couch.actions.settings["couch_window"] = "fullscreen"
        couch.actions.settings["couch_resolution"] = ""
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_store_shows_only_games_that_work_with_a_controller(ui, qtbot, routes):
    """Couch mode's Store lists only games Steam says have full or partial controller support (the default);
    the App ID comes from the site, or from Steam's search by the exact title."""
    import json
    from urllib.parse import parse_qs, urlparse
    import httpx
    from ezgd.catalog.models import Game
    from ezgd.ui.couch import T_STORE
    win, core, _ = ui
    searched = []

    def search(req):
        term = parse_qs(urlparse(str(req.url)).query)["term"][0]
        searched.append(term)
        items = {"Pad Game": [{"type": "app", "id": 100, "name": "Pad Game"}],
                 "Couch Party": [{"type": "app", "id": 300, "name": "Couch Party"}]}.get(term, [])
        return httpx.Response(200, json={"items": items})

    def items(req):
        payload = json.loads(parse_qs(urlparse(str(req.url)).query)["input_json"][0])
        cats = {100: [28], 200: [], 300: [18]}
        out = [{"appid": i["appid"], "success": 1, "name": f"app {i['appid']}",
                "categories": {"controller_categoryids": cats[i["appid"]]}}
               for i in payload["ids"] if i["appid"] in cats]
        return httpx.Response(200, json={"response": {"store_items": out}})
    routes.add_handler("https://store.steampowered.com/api/storesearch/*", search)
    routes.add_handler("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/*", items)
    core.rate_limiter.overrides["steampowered.com"] = 0.0     # (the app spaces Steam's search 1 s apart)
    pad = Game("fitgirl", "1", "Pad Game", "https://fitgirl.example/1")
    mouse = Game("onlinefix", "2", "Mouse Game", "https://onlinefix.example/2")
    mouse.steam_app_id = 200                                   # the site names it: no search needed
    party = Game("steamrip", "3", "Couch Party", "https://steamrip.example/3")
    nowhere = Game("crackedgames", "4", "Not On Steam", "https://crackedgames.example/4")
    for g in (pad, mouse, party, nowhere):
        core.games.save(g)
    couch = _open_couch(win, qtbot)
    couch._switch_tab(T_STORE)
    store = couch.pages[T_STORE]
    store.set_filter("all")
    qtbot.waitUntil(lambda: {g.title for g in store._grid_items()} >= {"Pad Game", "Couch Party"}
                    and not couch.checking_controllers(), timeout=15000)
    shown = {g.title for g in store._grid_items()}
    assert shown & {"Pad Game", "Couch Party", "Mouse Game", "Not On Steam"} == {"Pad Game", "Couch Party"}
    assert "Mouse Game" not in searched                       # its App ID came from the site
    assert "with controller support" in store._status()
    couch.grab()
    assert not couch._paint_failed
    titles = json.loads((core.paths.cache / "steam-titles.json").read_text())
    assert titles["pad game"][0] == 100 and titles["not on steam"][0] == 0        # remembered (misses too)
    couch.actions.settings["couch_controller_only"] = False    # Settings → Store: every game again
    store._vis.clear()
    assert {"Pad Game", "Mouse Game", "Couch Party", "Not On Steam"} <= {g.title for g in store._grid_items()}
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_couch_display_settings_ask_and_go_back(ui, qtbot):
    from ezgd.ui.couch import T_SETTINGS
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch._switch_tab(T_SETTINGS)
    page = couch.pages[T_SETTINGS]
    k = next(i for i, it in enumerate(page.items) if it.label == "Window")
    page.focus = k
    couch.command("right")                                     # Full screen → Borderless window
    assert couch.actions.settings["couch_window"] == "borderless"
    assert couch.overlay is not None and couch.overlay.tag == "display"
    assert not couch.isFullScreen()
    couch.grab()
    couch.command("right")                                     # → "Go back"
    couch.command("a")
    assert couch.actions.settings["couch_window"] == "fullscreen" and couch.overlay is None
    qtbot.waitUntil(lambda: core.settings.settings.couch_window == "fullscreen", timeout=5000)
    s = couch._s()
    page.focus = next(i for i, it in enumerate(page.items) if it.label == "Interface size")
    couch.command("right")                                     # 100 % → 110 %
    assert abs(couch._s() - s * 1.1) < 1e-6
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


@pytest.mark.parametrize("how", ["button", "escape", "alt-f4"])
def test_couch_browser_every_way_out_lands_in_couch_mode(ui, qtbot, server, how):
    """A friend got stuck in the browser (no controller: the desktop toolbar is hidden in couch mode) and closing it
    with a key combination left couch mode in a bad state."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from ezgd.catalog.models import Game
    from ezgd.ui import couch_pages
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    server.add("/d/w", data=b"<html><body>page</body></html>", content_type="text/html")
    g = Game("steamrip", "95", "Way Out", "https://steamrip.com/way-out/")
    core.games.save(g)
    couch = _open_couch(win, qtbot)
    couch_pages.open_browser(couch, g, None, server.url("/d/w"))
    cb = couch.browser
    qtbot.waitUntil(lambda: cb.dlg.current.url().path() == "/d/w", timeout=20000)
    assert couch.isVisible() and not couch.isMinimized()       # couch mode stays underneath
    assert cb.close_button.isVisible()
    if how == "button":
        QTest.mouseClick(cb.close_button, Qt.LeftButton)
    elif how == "escape":
        cb.open_keyboard()
        cb._esc.activated.emit()                               # first only the on-screen keyboard closes
        assert cb.overlay.keyboard is None and couch.browser is cb
        cb._esc.activated.emit()
    else:
        cb.dlg.close()                                         # the window manager's close (Alt+F4)
    qtbot.waitUntil(lambda: couch.browser is None, timeout=5000)
    assert not couch.pads.pointer_mode and couch.isVisible() and not couch.isMinimized()
    assert couch._accepts_input()
    couch.command("rb")                                        # the controller drives couch mode again
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_fix_my_game_shows_a_progress_bar_in_couch_mode(ui, qtbot):
    from ezgd.ui.couch import Overlay
    win, core, _ = ui
    couch = _open_couch(win, qtbot)
    couch.overlay = Overlay("Fixing G…", "", busy=True, tag="fixing:7", progress=0.0)
    win._on_event("fixmygame", {"id": 7, "step": "Testing", "status": "running", "detail": "did that fix it?",
                                "progress": 0.55})               # the core's report reaches couch mode
    assert couch.overlay.progress == 0.55 and "did that fix it?" in couch.overlay.text
    couch.fix_progress({"id": 8, "step": "x", "progress": 0.9})          # another game's: ignored
    assert couch.overlay.progress == 0.55
    couch.grab()
    assert not couch._paint_failed
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)


def test_an_error_report_opens_above_a_modal_window(ui, qtbot):
    """The report opened under the main window while Diagnose (modal) was open: it could not be clicked."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog
    from ezgd.core.crashreport import CrashReport
    from ezgd.ui.dialogs.crash import CrashNotifier
    win, core, _ = ui
    modal = QDialog(win)
    modal.setWindowModality(Qt.ApplicationModal)
    modal.show()
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is modal, timeout=3000)
    n = CrashNotifier(lambda: win)
    n._show(CrashReport("EZGD-PY-TEST01", "Something failed.", "full text"))
    assert n._open is not None and n._open.parent() is modal
    n._open.close()
    modal.close()


def test_a_torrent_from_the_couch_browser_shows_once_on_the_desktop(ui, qtbot, server, tmp_path):
    """A site's .torrent downloaded in couch mode's browser: the game downloads as an ordinary torrent. The finished
    .torrent file stayed listed too — the desktop's Downloads page showed the game twice after leaving couch mode —
    and the row added while the desktop was hidden came back squashed."""
    from PySide6.QtCore import QPointF
    from ezgd.catalog.models import Game
    from ezgd.ui import couch_pages
    from ezgd.ui.dialogs import browser
    from test_torrent import make_torrent
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    tor = make_torrent({"T.zip": b"x" * 5000}, tmp_path / "seed2", "TGame")
    server.add("/f/TGame.torrent", data=tor, content_type="application/x-bittorrent",
               headers={"Content-Disposition": 'attachment; filename="TGame.torrent"'})
    server.add("/d/t", data=b'<html><body style="margin:0"><a href="/f/TGame.torrent" style="display:block;'
                            b'width:100%;height:100vh">Download</a></body></html>', content_type="text/html")
    g = Game("onlinefix", "555", "T Game", "https://online-fix.me/games/x/555-t.html")
    core.games.save(g)
    couch = _open_couch(win, qtbot)
    couch_pages.open_browser(couch, g, None, server.url("/d/t"))
    cb = couch.browser
    qtbot.waitUntil(lambda: cb.dlg.current.url().path() == "/d/t", timeout=20000)
    qtbot.wait(500)
    cb.pointer = QPointF(cb.view_rect().center())
    cb.button("a", True)
    cb.button("a", False)
    qtbot.waitUntil(lambda: bool(core.downloads.tasks), timeout=20000)
    page = win.pages["downloads"]
    qtbot.waitUntil(lambda: bool(page.rows) and not page.browser_rows, timeout=10000)
    couch.force_close()
    qtbot.waitUntil(lambda: win.isVisible(), timeout=5000)
    win.navigate("downloads")
    qtbot.wait(200)
    assert [r.snap["title"] for _i, r in page.rows.values()] == ["T Game"] and not page.browser_rows
    item, row = next(iter(page.rows.values()))
    qtbot.waitUntil(lambda: row.height() >= row.sizeHint().height(), timeout=3000)   # (not squashed)


def test_couch_mode_never_starts_the_web_engine_to_look_for_browser_downloads(ui, qtbot, monkeypatch):
    """Couch mode asked the in-app browser for its downloads a second after opening — which started the whole web
    engine (150–200 ms frozen, mid-intro) when the browser had not been used at all."""
    from ezgd.ui.dialogs import browser
    win, core, _ = ui
    monkeypatch.setattr(browser, "_MANAGER", None)
    made = []
    monkeypatch.setattr(browser, "BrowserDownloads", lambda actions: made.append(actions))
    couch = _open_couch(win, qtbot)
    couch._poll_downloads()
    couch._poll_browser()
    assert made == [] and browser._MANAGER is None and couch.browser_downloads == []
    couch.force_close()
