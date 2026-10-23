"""UI tests (offscreen Qt): navigation, debounced search, responsiveness, details → download flow."""

import time

import httpx
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton

from conftest import fixture_bytes, make_torrent
from ezgd.catalog.models import Game, GameGroup

CP = "text/html; charset=windows-1251"


@pytest.fixture
def ui(qtbot, app_paths, routes, tmp_path):
    from ezgd.core.app_core import AppCore
    from ezgd.ui import theme
    from ezgd.ui.bridge import Bridge, CoreThread
    from ezgd.ui.main_window import MainWindow
    from test_torrent import LOCAL

    tor = make_torrent({"PEAK.zip": b"x" * 50_000}, tmp_path / "seed", "PEAK")
    routes.add("https://online-fix.me/", fixture_bytes("onlinefix_home.html"), content_type=CP)
    routes.add("https://online-fix.me/games/adventures/17801-peak-po-seti.html",
               fixture_bytes("onlinefix_detail_peak.html"), content_type=CP)
    routes.add("https://hosters.online-fix.me:2053/PEAK", fixture_bytes("onlinefix_hosters.html"))
    routes.add("https://uploads.online-fix.me:2053/torrents/PEAK/", fixture_bytes("onlinefix_torrent_index.html"))
    routes.add("https://uploads.online-fix.me:2053/torrents/PEAK/PEAK.v2.6.b-OFME.torrent", tor,
               content_type="application/x-bittorrent")
    routes.add("https://steamrip.com/wp-json/wp/v2/categories*", fixture_bytes("steamrip_categories.json"),
               content_type="application/json")
    routes.add("https://steamrip.com/wp-json/wp/v2/posts?*", fixture_bytes("steamrip_posts.json"),
               content_type="application/json", headers={"x-wp-totalpages": "1"})

    def search(req):
        time.sleep(float(ui_state["delay"]))
        ui_state["searches"] += 1
        return httpx.Response(200, content=fixture_bytes("onlinefix_search.html"), headers={"content-type": CP})
    ui_state = {"delay": 0, "searches": 0}
    routes.add_handler("https://online-fix.me/index.php*", search)
    routes.add("https://online-fix.me/uploads/*", b"", status=404)

    core = AppCore(app_paths, transport=httpx.MockTransport(routes), torrent_overrides=LOCAL)
    core.open_database()
    thread = CoreThread()
    thread.start()
    bridge = Bridge(thread, core)
    bridge.run_sync(core.start())
    bridge.run_sync(core.set_setting("download_dir", str(tmp_path / "dl")))
    bridge.run_sync(core.set_setting("install_dir", str(tmp_path / "games")))
    # this computer's real Steam must not be used (or restarted) by the tests: Play starts games directly here
    bridge.run_sync(core.set_setting("launch_through_steam", False))
    bridge.run_sync(core.set_setting("steam_overlay", False))
    from PySide6.QtWidgets import QApplication
    theme.apply_theme(QApplication.instance())
    win = MainWindow(bridge, app_paths, bridge.run_sync(core.get_settings()))
    # not qtbot.addWidget(win): pytest-qt would close it before teardown and, with a download still
    # active, the (intended) "quit while downloading?" prompt would block. force_quit() below skips it.
    win.show()
    yield win, core, ui_state
    win.force_quit()
    win.deleteLater()
    bridge.run_sync(core.shutdown(), 30)
    thread.stop()


def test_navigation_and_home(ui, qtbot):
    win, core, _ = ui
    for key in ("games", "picks", "downloads", "library", "settings", "home"):
        win.navigate(key)
        assert win.stack.currentWidget() is win.pages[key]
    qtbot.waitUntil(lambda: win.pages["home"].popular.model_.rowCount() > 0, timeout=10000)
    qtbot.waitUntil(lambda: win.pages["home"].recent.model_.rowCount() > 0, timeout=10000)


def test_picks_page_shows_real_sections(ui, qtbot):
    win, *_ = ui
    win.navigate("picks")
    page = win.pages["picks"]
    qtbot.waitUntil(lambda: len(page.grids) == 2, timeout=10000)
    assert page.grids[0].model_.groups[0].title == "How to Fish"


def test_debounced_search_and_responsiveness(ui, qtbot):
    win, core, state = ui
    win.navigate("games")
    page = win.pages["games"]
    qtbot.waitUntil(lambda: page.grid.model_.rowCount() > 0, timeout=10000)
    state["delay"] = 1.5                      # slow site
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(50)
    for ch in "dayz":                          # typing quickly → one request after the debounce
        qtbot.keyClicks(page.search, ch)
        qtbot.wait(60)
    qtbot.waitUntil(lambda: state["searches"] >= 1, timeout=10000)
    qtbot.waitUntil(lambda: any(g.title == "DayZ (DayZavr)" for g in page.grid.model_.groups), timeout=10000)
    timer.stop()
    assert state["searches"] == 1
    span = ticks[-1] - ticks[0]
    # the GUI kept processing timer events while the core thread waited on the network
    assert span > 1.2 and len(ticks) >= span / 0.05 * 0.6
    assert "game" in page.status.text()


def test_details_dialog_to_download(ui, qtbot, monkeypatch):
    win, core, _ = ui
    from ezgd.ui.dialogs.download_options import DownloadOptionsDialog
    from ezgd.ui.dialogs.game_details import GameDetailsDialog
    g = Game("onlinefix", "17801", "PEAK", "https://online-fix.me/games/adventures/17801-peak-po-seti.html")
    core.games.save(g)
    dlg = GameDetailsDialog(win.actions, GameGroup(g.dedup_key, [g]), win)
    qtbot.addWidget(dlg)
    dlg.show()

    def download_buttons():
        return [b for b in dlg.findChildren(QPushButton) if b.text() == "Download" and b.isVisible()]
    qtbot.waitUntil(lambda: len(download_buttons()) >= 1, timeout=10000)
    browser = [b for b in dlg.findChildren(QPushButton) if b.text() == "Open in browser"]
    assert browser, "external methods must offer the browser fallback"
    from ezgd.ui.widgets.common import PasswordBanner
    banners = dlg.findChildren(PasswordBanner)
    assert banners and "online-fix.me" in banners[0].text.text() and "OnlineFix" in banners[0].text.text()
    captured = {}

    def fake_exec(self):
        captured["prep"] = self.prep
        return DownloadOptionsDialog.Accepted
    monkeypatch.setattr(DownloadOptionsDialog, "exec", fake_exec)
    download_buttons()[0].click()             # the torrent (supported methods are listed first)
    qtbot.waitUntil(lambda: bool(win.pages["downloads"].rows), timeout=10000)
    assert captured["prep"]["kind"] == "torrent"
    snap = next(iter(win.pages["downloads"].rows.values()))[1].snap
    assert snap["title"] == "PEAK" and snap["kind"] == "torrent"
    assert snap["source"] == "onlinefix" and snap["archive_password"] == "online-fix.me"
    assert captured["prep"]["password"] == "online-fix.me"
    dlg.close()


def test_cards_name_the_source_and_password(ui, qtbot):
    win, *_ = ui
    from PySide6.QtCore import Qt
    from ezgd.ui.widgets.game_grid import GameListModel
    m = GameListModel()
    m.set_games([Game("onlinefix", "1", "A", "https://online-fix.me/x/1-a.html"),
                 Game("steamrip", "2", "B", "https://steamrip.com/b/")])
    of_tip = m.data(m.index(0), Qt.ToolTipRole)
    sr_tip = m.data(m.index(1), Qt.ToolTipRole)
    assert "Available from: OnlineFix" in of_tip and "password: online-fix.me" in of_tip
    assert "Available from: SteamRIP" in sr_tip and "password" not in sr_tip
    chip = win.pages["games"].source_buttons["onlinefix"]
    assert chip.text().startswith("🔒") and "online-fix.me" in chip.toolTip()


def test_settings_apply_live(ui, qtbot):
    win, core, _ = ui
    win.navigate("downloads")
    spin = win.pages["downloads"].concurrency
    spin.setValue(7)
    qtbot.waitUntil(lambda: core.settings.settings.max_concurrent_downloads == 7, timeout=5000)
    win.navigate("settings")
    qtbot.waitUntil(lambda: win.pages["settings"].widgets["max_concurrent_downloads"].value() == 7, timeout=5000)


def _buttons(widget, text):
    return [b for b in widget.findChildren(QPushButton) if b.text().strip() == text and b.isVisible()]


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="uses a POSIX script as the game")
def test_library_setup_play_and_stop(ui, qtbot, tmp_path, monkeypatch):
    win, core, _ = ui
    folder = tmp_path / "games" / "Cool"
    folder.mkdir(parents=True)
    game = folder / "cool.sh"
    game.write_text("#!/bin/sh\nsleep 30\n")
    game.chmod(0o755)
    entry = win.bridge.run_sync(core.library_call("import_folder", "Cool", str(folder)))
    win.navigate("library")
    page = win.pages["library"]
    qtbot.waitUntil(lambda: page.list.count() == 1, timeout=5000)
    page.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: bool(_buttons(page, "Set up")), timeout=5000)   # no executable yet

    from ezgd.ui.dialogs.properties import PropertiesDialog
    dlg = PropertiesDialog(win.actions, page.entries[entry.id], page.env, page)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: any("cool.sh" in dlg.exe.itemText(i) for i in range(dlg.exe.count())), timeout=5000)
    dlg.exe.setCurrentIndex(next(i for i in range(dlg.exe.count()) if "cool.sh" in dlg.exe.itemText(i)))
    dlg.options.setText("EZGD_TEST=1 %command% --windowed")
    qtbot.waitUntil(lambda: dlg.preview.text().endswith("cool.sh --windowed") and "EZGD_TEST=1" in dlg.preview.text(),
                    timeout=5000)
    dlg.options.setText('%command% "unterminated')
    qtbot.waitUntil(lambda: "syntax error" in dlg.preview.text(), timeout=5000)
    dlg.options.setText("EZGD_TEST=1 %command% --windowed")
    with qtbot.waitSignal(dlg.accepted, timeout=5000):
        dlg._save()
    saved = core.library.get(entry.id)
    assert saved.executable == str(game) and saved.launch_args == "EZGD_TEST=1 %command% --windowed"

    page.load()
    qtbot.waitUntil(lambda: bool(_buttons(page, "Play")), timeout=5000)
    from ezgd.ui.dialogs.misc import ConfirmLaunchDialog
    monkeypatch.setattr(ConfirmLaunchDialog, "exec", lambda self: ConfirmLaunchDialog.Accepted)
    _buttons(page, "Play")[0].click()
    qtbot.waitUntil(lambda: entry.id in page.running and bool(_buttons(page, "Stop")), timeout=8000)
    assert "Running" in page.list.item(0).text()
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    _buttons(page, "Stop")[0].click()
    qtbot.waitUntil(lambda: entry.id not in page.running and bool(_buttons(page, "Play")), timeout=10000)
    assert core.library.get(entry.id).play_count == 1


def test_fixes_dialog_is_one_click_away(ui, qtbot, tmp_path):
    win, core, _ = ui
    folder = tmp_path / "games" / "StS2"
    folder.mkdir(parents=True)
    for n in ("SlayTheSpire2.exe", "winmm.dll", "OnlineFix64.dll", "launch_vulkan.bat"):
        (folder / n).write_bytes(b"MZ")
    entry = win.bridge.run_sync(core.library_call("import_folder", "Slay the Spire 2", str(folder)))
    win.bridge.run_sync(core.library_call("set_executable", entry.id, str(folder / "SlayTheSpire2.exe")))
    win.navigate("library")
    page = win.pages["library"]
    qtbot.waitUntil(lambda: page.list.count() == 1, timeout=5000)
    page.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: page._fixes_btn.text().strip().startswith("Fixes ("), timeout=5000)
    assert "recommended" in page._fixes_btn.text()               # Vulkan is suggested, DLLs already on
    from ezgd.ui.dialogs.fixes import FixesDialog
    dlg = FixesDialog(win.actions, page.entries[entry.id], page)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: "godot_vulkan" in dlg.boxes, timeout=5000)
    assert dlg.boxes["game_dlls"].isChecked() and not dlg.boxes["godot_vulkan"].isChecked()
    dlg._enable_recommended()
    assert dlg.boxes["godot_vulkan"].isChecked()
    dlg.boxes["godot_opengl"].setChecked(True)                     # radio-like: Vulkan is unticked
    assert not dlg.boxes["godot_vulkan"].isChecked()
    dlg.boxes["godot_vulkan"].setChecked(True)
    qtbot.waitUntil(lambda: "--rendering-driver vulkan" in dlg.preview.text(), timeout=5000)
    with qtbot.waitSignal(dlg.accepted, timeout=5000):
        dlg._save()
    assert {"game_dlls", "godot_vulkan"} <= set(core.library.get(entry.id).compat_config.fixes)


def test_crackedgames_page_offers_import(ui, qtbot, monkeypatch, routes, tmp_path):
    import hashlib
    import io
    import os
    import zipfile
    from PySide6.QtWidgets import QFileDialog
    from ezgd.ui.dialogs.game_details import GameDetailsDialog
    from ezgd.ui.widgets.common import PasswordBanner
    win, core, _ = ui
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Hytale/Hytale.exe", b"MZ" + b"\0" * 100)
    data = buf.getvalue()
    page = fixture_bytes("crackedgames_detail_hytale.html").replace(
        b"c987d34df92f1e70995df89d632222d007dd42378b1adf6a4ddaef99ca5952ed", hashlib.sha256(data).hexdigest().encode())
    url = "https://cracked-games.org/hytale-free-download"
    routes.add(url, page)
    g = Game("crackedgames", "hytale-free-download", "Hytale", url)
    core.games.save(g)
    dlg = GameDetailsDialog(win.actions, GameGroup(g.dedup_key, [g]), win)
    qtbot.addWidget(dlg)
    dlg.show()

    def import_button():
        return next((b for b in dlg.findChildren(QPushButton) if b.text() == "Import downloaded file…"), None)
    qtbot.waitUntil(lambda: import_button() is not None, timeout=10000)
    assert not [b for b in dlg.findChildren(QPushButton) if b.text() == "Download"]   # never in-app
    banners = dlg.findChildren(PasswordBanner)
    assert len(banners) == 1
    text = banners[0].text.text()
    assert "Every Cracked-Games archive is password-protected" in text and "cracked-games.org" in text
    assert "download in your browser" in text            # not "the app enters it automatically"
    shown = []
    monkeypatch.setattr(win.actions.toasts, "show", lambda title, text="", kind="info", *a: shown.append(title))
    import ezgd.ui.dialogs.game_details as gd
    monkeypatch.setattr(gd, "open_url", lambda url: None)
    next(b for b in dlg.findChildren(QPushButton) if b.text() == "Open in browser").click()
    assert shown == ["🔒 Archive password: cracked-games.org"]
    if os.environ.get("EZGD_SHOTS"):
        qtbot.wait(600)
        dlg.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "crackedgames_details.png"))
    f = tmp_path / "Hytale.zip"
    f.write_bytes(data)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(f), "")))
    import_button().click()
    qtbot.waitUntil(lambda: any(e.game_id == g.id for e in core.library.all()), timeout=10000)
    e = next(e for e in core.library.all() if e.game_id == g.id)
    assert "SHA-256 verified" in (e.notes or "")


def test_in_app_browser_download_to_library(ui, qtbot, server, tmp_path):
    """The pop-up is only for the user's check and click: once the site's download starts it hides itself, the
    transfer continues on the Downloads page and the file is added to the library automatically. Pop-ups to
    unrelated sites are blocked."""
    import io
    import os
    import zipfile
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Game/Game.exe", b"MZ" + b"\0" * 100)
    server.add("/file/Game.zip", data=buf.getvalue(), content_type="application/zip", chunk_delay=0.05,
               headers={"Content-Disposition": 'attachment; filename="Game.zip"'})
    server.add("/d/abc", data=b'<html><body><a id="dl" href="/file/Game.zip">Download</a>'
                              b'<script>window.open("https://ads.example/x")</script></body></html>',
               content_type="text/html")
    g = Game("steamrip", "77", "Browser Game", "https://steamrip.com/browser-game/")
    core.games.save(g)
    browser.open_browser_download(win.actions, g, server.url("/d/abc"), "TestHost", win)
    dlg = next(w for w in win.findChildren(browser.BrowserDownloadDialog) if w.game.id == g.id)
    loaded = []
    dlg.view.loadFinished.connect(loaded.append)
    qtbot.waitUntil(lambda: bool(loaded), timeout=20000)
    dlg.page.runJavaScript("document.getElementById('dl').click();")      # what the user's click does
    qtbot.waitUntil(lambda: bool(dlg.started), timeout=20000)
    qtbot.waitUntil(lambda: not dlg.isVisible(), timeout=5000)            # pop-up gone, transfer continues
    page = win.pages["downloads"]
    qtbot.waitUntil(lambda: bool(page.browser_rows), timeout=5000)
    qtbot.waitUntil(lambda: any(e.game_id == g.id for e in core.library.all()), timeout=20000)
    e = next(e for e in core.library.all() if e.game_id == g.id)
    m = browser.manager(win.actions)
    [d] = [d for d in m.items.values() if d.game.id == g.id]
    assert d.state == "completed" and e.download_path == str(d.path) and d.path.name == "Game.zip"
    assert d.path.parent.name == "Browser Game"                                      # the game's own folder   # the game's own folder
    qtbot.waitUntil(lambda: core.library.get(e.id).install_path is not None and not d.path.exists(),
                    timeout=20000)                       # extracted, then the archive is gone
    qtbot.waitUntil(lambda: "added to your Library" in next(iter(page.browser_rows.values())).info.text(),
                    timeout=5000)
    if os.environ.get("EZGD_SHOTS"):
        win.navigate("downloads")
        qtbot.wait(300)
        win.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "browser_downloads.png"))


def test_in_app_browser_download_pause_resume_cancel(ui, qtbot, server):
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    data = bytes(range(256)) * 4096                                  # 1 MiB in 64 chunks of 16 KiB
    for name in ("slow.bin", "other.bin"):
        server.add(f"/file/{name}", data=data, chunk_delay=0.05, content_type="application/octet-stream",
                   headers={"Content-Disposition": f'attachment; filename="{name}"'})
    server.add("/d/p", data=b'<a id="a" href="/file/slow.bin">a</a><a id="b" href="/file/other.bin">b</a>',
               content_type="text/html")
    g = Game("steamrip", "78", "Pause Game", "https://steamrip.com/pause-game/")
    core.games.save(g)
    browser.open_browser_download(win.actions, g, server.url("/d/p"), "TestHost", win)
    dlg = next(w for w in win.findChildren(browser.BrowserDownloadDialog) if w.game.id == g.id)
    loaded = []
    dlg.view.loadFinished.connect(loaded.append)
    qtbot.waitUntil(lambda: bool(loaded), timeout=20000)
    dlg.page.runJavaScript("document.getElementById('a').click();")
    qtbot.waitUntil(lambda: len(dlg.started) == 1 and dlg.started[0].req.receivedBytes() > 0, timeout=20000)
    m = browser.manager(win.actions)
    d = dlg.started[0]
    m.pause(d.id)
    qtbot.waitUntil(lambda: d.state == "paused", timeout=5000)
    qtbot.wait(400)
    got = d.req.receivedBytes()
    qtbot.wait(600)
    assert d.req.receivedBytes() - got < 64 * 1024                    # (at most what was already buffered)
    m.resume(d.id)
    qtbot.waitUntil(lambda: d.state == "completed", timeout=30000)
    assert d.path.read_bytes() == data and "game" not in d.library      # not an archive: kept, not imported
    # a second file from the same game page can be cancelled from the Downloads page
    dlg2 = browser.BrowserDownloadDialog(win.actions, g, server.url("/d/p"), "TestHost", win)
    dlg2.show()
    loaded2 = []
    dlg2.view.loadFinished.connect(loaded2.append)
    qtbot.waitUntil(lambda: bool(loaded2), timeout=20000)
    dlg2.page.runJavaScript("document.getElementById('b').click();")
    qtbot.waitUntil(lambda: bool(dlg2.started), timeout=20000)
    m.cancel(dlg2.started[0].id)
    qtbot.waitUntil(lambda: dlg2.started[0].state == "cancelled", timeout=5000)
    assert m.active() == 0


def test_download_all_parts_button(ui, qtbot, monkeypatch, routes):
    import json as _json
    from ezgd.catalog.models import DownloadMethod, MethodKind
    from ezgd.ui.dialogs.download_options import DownloadOptionsDialog
    from ezgd.ui.dialogs.game_details import GameDetailsDialog
    win, core, _ = ui
    names = [f"Parts.Game.v1-OFME.part{i}.rar" for i in (1, 2, 3)]
    for i, n in enumerate(names):
        routes.add(f"https://pixeldrain.com/api/file/pd{i}abc/info",
                   _json.dumps({"success": True, "name": n, "size": 1000, "can_download": True,
                                "availability": ""}), content_type="application/json")
        routes.add(f"https://pixeldrain.com/api/file/pd{i}abc?download", b"R" * 1000,
                   content_type="application/octet-stream")
    g = Game("onlinefix", "4242", "Parts Game", "https://online-fix.me/games/x/4242-parts.html",
             download_methods=[DownloadMethod(MethodKind.DIRECT, f"Pixeldrain — {n}", f"https://pixeldrain.com/u/pd{i}abc",
                                              host="pixeldrain.com", supported=True, resolver="pixeldrain", file_name=n,
                                              size_bytes=1000) for i, n in enumerate(names)])
    g.details_checked_at = 1e12
    core.games.save(g)

    async def same(gid, force=False):
        return core.games.get(gid)
    monkeypatch.setattr(core, "details", same)
    shown = []
    monkeypatch.setattr(DownloadOptionsDialog, "exec", lambda self: (shown.append(self.prep), 1)[1])
    dlg = GameDetailsDialog(win.actions, GameGroup(g.dedup_key, [g]), win)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: any(b.text() == "Download all 3 parts" for b in dlg.findChildren(QPushButton)),
                    timeout=10000)
    toasts = []
    monkeypatch.setattr(win.actions.toasts, "show", lambda *a, **k: toasts.append(a))
    next(b for b in dlg.findChildren(QPushButton) if b.text() == "Download all 3 parts").click()
    qtbot.waitUntil(lambda: len([t for t in core.downloads.tasks.values() if t.game_id == g.id]) == 3
                    or any(t[-1] == "error" for t in toasts if len(t) > 2), timeout=15000)
    assert not [t for t in toasts if len(t) > 2 and t[-1] == "error"], toasts
    assert len(shown) == 1                                         # options confirmed once for all parts
    assert sorted(t.filename for t in core.downloads.tasks.values() if t.game_id == g.id) == names


def test_buttons_do_not_pass_qt_checked_flag(qtbot):
    """Handlers written as `lambda k=value: …` (Apply / Fix it / Use this / Download all parts) must get their
    own value, not Qt's checked=False — that silently broke several buttons."""
    from ezgd.ui.widgets.common import Banner, button
    got = []
    b = button("x", on_click=lambda k="key": got.append(k))
    qtbot.addWidget(b)
    b.click()
    banner = Banner("info", "t")
    qtbot.addWidget(banner)
    banner.add_action("Apply", lambda k="apply-key": got.append(k)).click()
    assert got == ["key", "apply-key"]


def _click_element(qtbot, dlg, element_id: str) -> None:
    """A real mouse click (user gesture) on an element of the pop-up's current page."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    rect = []
    dlg.current.runJavaScript(
        f"(function(){{var r=document.getElementById('{element_id}').getBoundingClientRect();"
        f"return (r.left + r.width / 2) + ',' + (r.top + r.height / 2);}})()", 0, rect.append)
    qtbot.waitUntil(lambda: bool(rect), timeout=10000)
    x, y = (float(v) for v in str(rect[0]).split(","))
    target = dlg.view.focusProxy() or dlg.view
    QTest.mouseClick(target, Qt.LeftButton, Qt.NoModifier, QPoint(int(x), int(y)))


def test_in_app_browser_follows_clicks_into_other_sites(ui, qtbot, server):
    """SteamRIP-style pages open the host in a new tab on another site: the user's click must work (that was
    blocked as a pop-up); script pop-ups stay blocked; program downloads (ads) are refused."""
    import io
    import zipfile
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    other = server.base.replace("127.0.0.1", "localhost")              # a different site
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Click Game/Game.exe", b"MZ" + b"\0" * 100)
    server.add("/f/Click.Game.zip", data=buf.getvalue(), content_type="application/zip",
               headers={"Content-Disposition": 'attachment; filename="Click.Game.zip"'})
    server.add("/f/setup.exe", data=b"MZ fake", content_type="application/octet-stream",
               headers={"Content-Disposition": 'attachment; filename="FastDownloader_setup.exe"'})
    big = 'style="display:block;width:400px;height:60px;font-size:30px"'
    server.add("/host/file", content_type="text/html", data=(
        f'<html><body><a id="ad" {big} href="/f/setup.exe">DOWNLOAD (ad)</a>'
        f'<a id="real" {big} href="/f/Click.Game.zip">Download File</a></body></html>').encode())
    server.add("/game", content_type="text/html", data=(
        f'<html><body><a id="host" {big} target="_blank" href="{other}/host/file">Buzzheavier</a>'
        '<script>setTimeout(function(){window.open("https://ads.example/pop")}, 300)</script></body></html>'
    ).encode())
    g = Game("steamrip", "79", "Click Game", "https://steamrip.com/click-game/")
    core.games.save(g)
    browser.open_browser_download(win.actions, g, server.url("/game"), "Buzzheavier", win)
    dlg = next(w for w in win.findChildren(browser.BrowserDownloadDialog) if w.game.id == g.id)
    dlg.resize(1000, 700)
    loaded = []
    dlg.view.loadFinished.connect(loaded.append)
    qtbot.waitUntil(lambda: bool(loaded), timeout=20000)
    qtbot.waitUntil(lambda: dlg.notice.isVisible() and "Blocked a pop-up" in dlg.notice.text.text(), timeout=5000)
    _click_element(qtbot, dlg, "host")                                 # the user's click into the host site
    qtbot.waitUntil(lambda: dlg.current.url().toString().endswith("/host/file"), timeout=10000)
    assert len(dlg.tabs) == 2
    loaded.clear()
    qtbot.waitUntil(lambda: dlg.current.url().host() == "localhost", timeout=5000)
    qtbot.wait(500)
    _click_element(qtbot, dlg, "ad")                                   # a fake download button
    qtbot.waitUntil(lambda: "is a program, not a game archive" in dlg.notice.text.text(), timeout=10000)
    assert not dlg.started
    _click_element(qtbot, dlg, "real")
    qtbot.waitUntil(lambda: bool(dlg.started), timeout=10000)
    qtbot.waitUntil(lambda: any(e.game_id == g.id for e in core.library.all()), timeout=20000)
    dlg.back() if dlg.isVisible() else None


def test_uninstall_dialog(ui, qtbot, monkeypatch, tmp_path):
    import os
    from PySide6.QtWidgets import QMessageBox
    from ezgd.ui.dialogs.uninstall import UninstallDialog
    win, core, _ = ui
    games = tmp_path / "games" / "Old Game"
    games.mkdir(parents=True)
    (games / "game.exe").write_bytes(b"MZ" + b"\0" * 20000)
    e = core.library.import_folder("Old Game", games)
    prefix = core.library.prefix_dir(e.id) / "pfx"
    prefix.mkdir(parents=True)
    (prefix / "save.dat").write_bytes(b"S" * 100)
    dlg = UninstallDialog(win.actions, e, win)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: len(dlg.boxes) >= 2, timeout=10000)
    assert dlg.boxes["files"].isChecked() and dlg.boxes["prefix"].isChecked() and dlg.go.isEnabled()
    if os.environ.get("EZGD_SHOTS"):
        qtbot.wait(300)
        dlg.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "uninstall.png"))
    dlg.boxes["prefix"].setChecked(False)                         # keep the saves
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    dlg.go.click()
    qtbot.waitUntil(lambda: dlg.result_info is not None, timeout=10000)
    assert not games.exists() and prefix.exists() and core.library.repo.get(e.id) is None


def test_in_app_browser_ad_blocker(ui, qtbot, server):
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    other = server.base.replace("127.0.0.1", "localhost")              # an "ad" site that is on no list
    big = 'style="display:block;width:400px;height:60px;font-size:30px"'
    server.add("/next", content_type="text/html", data=b"<html><body>next page</body></html>")
    server.add("/ads", content_type="text/html", data=(
        '<html><body><script src="http://ads.popads.net/pop.js"></script>'
        f'<div id="area" {big} onclick="window.open(\'{other}/ad\')">click anywhere</div>'
        f'<a id="link" {big} href="/next" onclick="window.open(\'{other}/ad2\')">Real link</a>'
        '</body></html>').encode())
    g = Game("steamrip", "80", "Ad Game", "https://steamrip.com/ad-game/")
    core.games.save(g)
    browser.open_browser_download(win.actions, g, server.url("/ads"), "Host", win)
    dlg = next(w for w in win.findChildren(browser.BrowserDownloadDialog) if w.game.id == g.id)
    dlg.resize(1000, 700)
    loaded = []
    dlg.view.loadFinished.connect(loaded.append)
    qtbot.waitUntil(lambda: bool(loaded), timeout=20000)
    assert dlg.shield.isChecked()
    qtbot.waitUntil(lambda: browser.ADBLOCK.count("127.0.0.1") >= 1, timeout=15000)   # popads.net script blocked
    _click_element(qtbot, dlg, "area")                                 # pop-under on a click on the page
    qtbot.waitUntil(lambda: "pop-under" in dlg.notice.text.text(), timeout=15000)
    assert len(dlg.tabs) == 1
    dlg.notice.hide()
    _click_element(qtbot, dlg, "link")                                 # real link + hijacked pop-up
    qtbot.waitUntil(lambda: dlg.current.url().toString().endswith("/next"), timeout=10000)
    qtbot.wait(300)
    assert len(dlg.tabs) == 1 and "pop-under" in dlg.notice.text.text()
    qtbot.waitUntil(lambda: "blocked" in dlg.shield_count.text(), timeout=15000)
    # switched off, the same click is allowed (for sites that break with blocking)
    dlg.shield.setChecked(False)
    dlg.current.load(server.url("/ads"))
    loaded.clear()
    qtbot.waitUntil(lambda: bool(loaded), timeout=20000)
    _click_element(qtbot, dlg, "area")
    qtbot.waitUntil(lambda: len(dlg.tabs) == 2, timeout=15000)
    dlg.shield.setChecked(True)


def test_add_site_dialog(ui, qtbot, routes):
    import json as _json
    import os
    from ezgd.ui.dialogs.add_site import AddSiteDialog
    from test_custom_sources import BASE, game_post, site
    win, core, _ = ui
    dlg = AddSiteDialog(win.actions, win)
    qtbot.addWidget(dlg)
    dlg.show()
    dlg.url.setText("https://www.youtube.com/@somechannel")
    dlg.go.click()
    qtbot.waitUntil(lambda: dlg.error.isVisible(), timeout=10000)
    assert "can't be added" in dlg.error.text() and "video site" in dlg.error.text()
    from ezgd.ui import theme as _theme
    assert _theme.CURRENT.danger in dlg.error.styleSheet()
    if os.environ.get("EZGD_SHOTS"):
        qtbot.wait(200)
        dlg.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "add_site_error.png"))
    site(routes, [game_post(i) for i in range(1, 6)])
    dlg.url.setText(BASE)
    dlg.go.click()
    qtbot.waitUntil(lambda: dlg.ok.isVisible(), timeout=10000)
    assert not dlg.error.isVisible() and "Added Example Repacks" in dlg.ok.text()
    qtbot.waitUntil(lambda: "site:repacks.example" in win.pages["games"].source_buttons, timeout=10000)
    _ = _json


def test_diagnose_dialog(ui, qtbot, tmp_path):
    import os
    from ezgd.ui.dialogs.diagnose import DiagnoseDialog
    from test_diagnose import pe
    win, core, _ = ui
    folder = tmp_path / "games" / "Broken Game"
    folder.mkdir(parents=True)
    pe(folder / "BrokenGame.exe")
    e = core.library.import_folder("Broken Game", folder)
    core.library.set_executable(e.id, folder / "BrokenGame.exe")
    (core.paths.logs / f"game-{e.id}.log").write_text(
        "\n=== 2026-10-10 10:00:00 Proton-GE Latest · Steam Linux Runtime 4.0\ncmd\n"
        '0124:err:module:import_dll Library MSVCP140.dll (which is needed by L"X:\\\\Broken Game\\\\BrokenGame.exe")'
        ' not found\n=== exited with code 1 after 2s\n')
    dlg = DiagnoseDialog(win.actions, core.library.get(e.id), win)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: "Most likely cause" in dlg.verdict_text, timeout=10000)
    assert "exited with code 1 after 2s" in dlg.verdict_text and "2s" in dlg.summary.text()
    buttons = [b.text() for b in dlg.findChildren(QPushButton)]
    assert "Install Microsoft Visual C++ 2015–2022 runtime" in buttons and "More" in buttons
    assert any("diagnostic launch" in a.text() for a in dlg.more_btn.menu().actions())   # the rarer tools
    if os.environ.get("EZGD_SHOTS"):
        qtbot.wait(300)
        dlg.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "diagnose.png"))


def test_try_fixes_flow(ui, qtbot, tmp_path, monkeypatch):
    import os
    from PySide6.QtWidgets import QMessageBox
    from ezgd.ui.dialogs.diagnose import DiagnoseDialog
    from test_diagnose import pe
    win, core, _ = ui
    folder = tmp_path / "games" / "Trial Game"
    folder.mkdir(parents=True)
    pe(folder / "TrialGame-Win64-Shipping.exe")
    e = core.library.import_folder("Trial Game", folder)
    core.library.set_executable(e.id, folder / "TrialGame-Win64-Shipping.exe")
    (core.paths.logs / f"game-{e.id}.log").write_text(
        "\n=== 2026-10-10 10:00:00 Proton\nD3D12CreateDevice failed\n=== exited with code 1 after 3s\n")

    asked = []

    async def fake_trials(entry_id, findings, generated=None, symptom=""):
        asked.append(symptom)                 # (the reported problem goes with the trials: they watch for it)
        results = [
            {"label": "Current setup (Proton)", "overrides": {}, "outcome": "crashed", "seconds": 3,
             "detail": "stopped after 3s: access violation"},
            {"label": "Start with DirectX 11", "overrides": {"launch_args": "-dx11"}, "outcome": "ran", "seconds": 45,
             "detail": "kept running for 45s without crashing"}]
        for n, r in enumerate(results, 1):
            core.emit("trial", {"id": entry_id, "index": n, "total": 2, "label": r["label"], "status": "running"})
            core.emit("trial", {"id": entry_id, "index": n, "total": 2, "label": r["label"], "status": "done",
                                "result": r})
        return results
    monkeypatch.setattr(core, "run_trials", fake_trials)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dlg = DiagnoseDialog(win.actions, core.library.get(e.id), win)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: "Most likely cause" in dlg.verdict_text, timeout=10000)
    dlg.try_btn.click()
    qtbot.waitUntil(lambda: "1 setup(s) kept the game running" in dlg.summary.text(), timeout=10000)
    texts = [w.text() for w in dlg._trial_rows.values()]
    assert asked == [""]
    assert any(t.startswith("✗ Current setup") for t in texts) and any(t.startswith("✓ Start with DirectX 11")
                                                                       for t in texts)
    if os.environ.get("EZGD_SHOTS"):
        qtbot.wait(300)
        dlg.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "trials.png"))
    use = next(b for b in dlg.findChildren(QPushButton) if b.text() == "Use this setup")
    use.click()
    qtbot.waitUntil(lambda: core.library.get(e.id).launch_args == "-dx11", timeout=5000)


def test_generated_fixes_shown(ui, qtbot, tmp_path, monkeypatch):
    import os
    from ezgd.platform import syscheck
    from ezgd.ui.dialogs.diagnose import DiagnoseDialog
    from pe_builder import build_pe
    win, core, _ = ui
    g = tmp_path / "games" / "Dark Game"
    (g / "Dark_Data").mkdir(parents=True)
    (g / "Dark.exe").write_bytes(build_pe(["kernel32.dll"]))
    (g / "UnityPlayer.dll").write_bytes(build_pe(["kernel32.dll"]))
    e = core.library.import_folder("Dark Game", g)
    core.library.set_executable(e.id, g / "Dark.exe")
    monkeypatch.setattr(syscheck, "collect", lambda *a, **k: syscheck.SystemFacts(gpus=[
        syscheck.GPU("NVIDIA GeForce RTX 4090", "615"), syscheck.GPU("Intel(R) Graphics", "Mesa")]))
    dlg = DiagnoseDialog(win.actions, core.library.get(e.id), win)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: "Reading" not in dlg.summary.text(), timeout=10000)
    dlg.symptom.setCurrentIndex(dlg.symptom.findData("black_screen"))
    qtbot.waitUntil(lambda: "built" in dlg.verdict_text, timeout=10000)
    labels = [w.text() for w in dlg.findChildren(type(dlg.summary))]
    assert any("Force the NVIDIA GeForce RTX 4090" in t for t in labels)
    assert any("DXVK_FILTER_DEVICE_NAME=GeForce RTX 4090" in t for t in labels)
    assert any(b.text() == "Test them" for b in dlg.findChildren(QPushButton))
    if os.environ.get("EZGD_SHOTS"):
        qtbot.wait(300)
        dlg.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "generated.png"))


def test_fix_my_game_flow(ui, qtbot, tmp_path, monkeypatch):
    import os
    from PySide6.QtWidgets import QMessageBox
    from ezgd.ui.dialogs.diagnose import DiagnoseDialog
    from pe_builder import build_pe
    win, core, _ = ui
    g = tmp_path / "games" / "Fixable"
    g.mkdir(parents=True)
    (g / "Fixable.exe").write_bytes(build_pe(["kernel32.dll"]))
    e = core.library.import_folder("Fixable", g)
    core.library.set_executable(e.id, g / "Fixable.exe")

    async def fake(entry_id, symptom="", test=True):
        for name, st, detail in [("Checking the game", "running", ""), ("Checking the game", "done", "2 problems"),
                                 ("Repairing", "running", "✓ 3 game file(s) are missing or damaged"),
                                 ("Repairing", "done", "2 repair(s)"), ("Testing", "running", ""),
                                 ("Testing", "done", "✓ Start with DirectX 11 kept the game running")]:
            core.emit("fixmygame", {"id": entry_id, "step": name, "status": st, "detail": detail})
        return {"status": "fixed", "kept": "Start with DirectX 11",
                "applied": ["3 game file(s) are missing or damaged — Restored 3 file(s) from the archive.",
                            "The CODEX Steam emulator is set up with the wrong App ID — Changed the setting."],
                "before": {"launch_args": ""}, "findings": []}
    monkeypatch.setattr(core, "fix_my_game", fake)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dlg = DiagnoseDialog(win.actions, core.library.get(e.id), win)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: "Reading" not in dlg.summary.text(), timeout=10000)
    dlg.fix_btn.click()
    qtbot.waitUntil(lambda: dlg.summary.text() == "Done.", timeout=10000)
    texts = [w.text() for w in dlg.findChildren(type(dlg.summary))]
    assert any(t.startswith("✓ Fixed — Fixable kept running with: Start with DirectX 11") for t in texts)
    assert any("Restored 3 file(s)" in t for t in texts)
    if os.environ.get("EZGD_SHOTS"):
        qtbot.wait(300)
        dlg.grab().save(os.path.join(os.environ["EZGD_SHOTS"], "fixmygame.png"))
    next(b for b in dlg.findChildren(QPushButton) if b.text() == "Yes, it works").click()
    qtbot.waitUntil(lambda: "fixed for you: 1 of 1 (100%)" in dlg.stats.text(), timeout=10000)


def test_garbage_collection_only_on_main_thread(qtbot):
    """Regression (core dumps 2026-10-10): automatic GC on a worker thread crashed while the main thread deleted
    widgets. The guard turns automatic collection off and collects on a main-thread timer."""
    import gc
    import threading
    from PySide6.QtWidgets import QApplication
    from ezgd.ui import gcguard
    seen = []
    cb = lambda phase, info: phase == "start" and seen.append(threading.current_thread() is threading.main_thread())
    gc.callbacks.append(cb)
    try:
        guard = gcguard.install(QApplication.instance())
        assert not gc.isenabled()

        def churn():
            for _ in range(200000):
                a = {}
                a["self"] = a                       # cyclic garbage that would trigger automatic collection
        t = threading.Thread(target=churn)
        t.start()
        t.join()
        qtbot.wait(1200)
        assert seen and all(seen)                   # collections happened, and only on the main thread
        guard.stop()
    finally:
        gc.callbacks.remove(cb)


def test_rows_do_not_hijack_vertical_scrolling(ui, qtbot):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget
    from ezgd.ui.widgets.game_grid import GameGridView
    win, core, _ = ui
    page = QScrollArea()
    page.setWidgetResizable(True)
    body = QWidget()
    lay = QVBoxLayout(body)
    lay.addWidget(QLabel("top"))
    row = GameGridView(win.actions.covers, horizontal=True)
    games = [Game("steamrip", str(i), f"Game {i}", f"https://steamrip.com/{i}/") for i in range(40)]
    row.set_groups([GameGroup(g.dedup_key, [g]) for g in games])
    lay.addWidget(row)
    spacer = QLabel("x")
    spacer.setFixedHeight(3000)
    lay.addWidget(spacer)
    page.setWidget(body)
    page.resize(900, 600)
    qtbot.addWidget(page)
    page.show()
    qtbot.wait(200)
    hsb, vsb = row.horizontalScrollBar(), page.verticalScrollBar()
    assert hsb.maximum() > 0 and vsb.value() == 0

    def wheel(dy, mods=Qt.NoModifier):
        return QWheelEvent(QPointF(200, 40), QPointF(row.viewport().mapToGlobal(QPoint(200, 40))), QPoint(0, 0),
                           QPoint(0, dy), Qt.NoButton, mods, Qt.NoScrollPhase, False)
    ev = wheel(-240)
    row.wheelEvent(ev)                                  # plain wheel over the row: the row declines it…
    assert not ev.isAccepted() and hsb.value() == 0
    QApplication.sendEvent(page.viewport(), wheel(-240))   # …so it reaches the page, which scrolls down
    assert vsb.value() > 0
    # over the row's scroll bar (2026-10-11: scrolling down Home got caught on it): the page scrolls, not the bar
    before = vsb.value()
    assert row.eventFilter(hsb, wheel(-240)) is True     # the bar never gets the plain wheel…
    assert hsb.value() == 0 and vsb.value() > before    # …it goes to the page instead
    QApplication.sendEvent(hsb, wheel(-240))            # and through Qt's own event delivery
    assert hsb.value() == 0
    assert "height: 14px" in hsb.styleSheet() and row.height() > row.delegate.card_size().height() + 16
    hsb.setValue(hsb.maximum() // 2)                    # the bar itself still works by dragging/clicking
    assert hsb.value() > 0
    hsb.setValue(0)
    ev = wheel(-240, Qt.ShiftModifier)                  # Shift + wheel: the row scrolls sideways
    row.wheelEvent(ev)
    assert ev.isAccepted() and hsb.value() > 0
    hsb.setValue(0)
    qtbot.wait(50)
    left, right = row._arrows
    assert right.isVisible() and not left.isVisible()
    right.click()
    qtbot.waitUntil(lambda: hsb.value() > 0, timeout=3000)
    qtbot.waitUntil(lambda: left.isVisible(), timeout=3000)


def test_slow_source_does_not_hold_up_the_others(ui, qtbot, routes):
    """Each source loads on its own (FitGirl's API can take 15 s): the fast sources show up first."""
    win, core, _ = ui

    async def slow_posts(req):
        import asyncio
        await asyncio.sleep(3)                     # a slow site (it never blocks the app's other requests)
        return httpx.Response(200, content=fixture_bytes("steamrip_posts.json"),
                              headers={"content-type": "application/json", "x-wp-totalpages": "1"})
    routes.add_handler("https://steamrip.com/wp-json/wp/v2/posts?*", slow_posts)
    win.navigate("games")
    page = win.pages["games"]
    page.reload()
    qtbot.waitUntil(lambda: any("onlinefix" in g.sources for g in page.grid.model_.groups), timeout=5000)
    assert "steamrip" in page.inflight and "loading" in page.status.text()
    qtbot.waitUntil(lambda: "steamrip" not in page.inflight, timeout=10000)
    assert any("steamrip" in g.sources for g in page.grid.model_.groups)


def _open_browser(win, core, qtbot, url: str, gid: str):
    from ezgd.ui.dialogs import browser
    g = Game("steamrip", gid, f"Game {gid}", f"https://steamrip.com/{gid}/")
    core.games.save(g)
    browser.open_browser_download(win.actions, g, url, "Host", win)
    dlg = next(w for w in win.findChildren(browser.BrowserDownloadDialog) if w.game.id == g.id)
    dlg.resize(1000, 700)
    loaded = []
    dlg.view.loadFinished.connect(loaded.append)
    qtbot.waitUntil(lambda: bool(loaded), timeout=20000)
    return dlg


def test_in_app_browser_cannot_be_locked(ui, qtbot, server, monkeypatch):
    """2026-10-11: the browser 'froze' (only Alt+F4 helped) — a page's alert()/confirm() opened a modal box that
    blocked the window, or it went full screen. Messages now go to the notice bar; a hung page can be stopped."""
    from PySide6.QtWidgets import QApplication
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    big = 'style="display:block;width:400px;height:60px;font-size:30px"'
    server.add("/trap", content_type="text/html", data=(
        "<html><body><script>alert('You won a prize!'); var r = confirm('Leave this page?');"
        "document.title = 'confirm=' + r;</script>"
        f"<div id='fs' {big} onclick=\"document.documentElement.requestFullscreen().catch(function(){{}})\">x</div>"
        f"<div id='hang' {big} onclick='setTimeout(function(){{while(true){{}}}}, 50)'>hang</div>"
        "</body></html>").encode())
    dlg = _open_browser(win, core, qtbot, server.url("/trap"), "81")
    qtbot.waitUntil(lambda: "You won a prize" in dlg.notice.text.text() or "Leave this page" in
                    dlg.notice.text.text(), timeout=10000)
    assert QApplication.activeModalWidget() is None                      # nothing modal: Cancel keeps working
    qtbot.waitUntil(lambda: dlg.current.title() == "confirm=false", timeout=5000)
    _click_element(qtbot, dlg, "fs")
    qtbot.wait(500)
    assert not dlg.isFullScreen() and not dlg.view.isFullScreen()
    monkeypatch.setattr(browser, "HANG_SECONDS", 1.5)
    dlg._watch.setInterval(300)
    _click_element(qtbot, dlg, "hang")                                  # the page's script never returns
    qtbot.waitUntil(lambda: "not responding" in dlg.notice.text.text(), timeout=15000)
    hung = dlg.current
    dlg._replace_current(False)                                         # "Stop this page"
    assert dlg.current is not hung and not dlg.notice.isVisible()
    import shiboken6
    dlg.close()                                                         # and Cancel closes the window
    qtbot.waitUntil(lambda: not shiboken6.isValid(dlg) or not dlg.isVisible(), timeout=5000)


def test_in_app_browser_blocks_mousedown_and_blank_popups(ui, qtbot, server):
    """Pop-unders that fire on mouse-down (before the click is known), blank pop-ups sent to an ad by script,
    'tab-under' redirects and filter-list (EasyList) scripts."""
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    other = server.base.replace("127.0.0.1", "localhost")              # an ad site on no domain list
    big = 'style="display:block;width:400px;height:60px;font-size:30px"'
    server.add("/next", content_type="text/html", data=b"<html><body>next page</body></html>")
    server.add("/js/popunder.js", content_type="application/javascript", data=b"window.__ad = 1;")
    server.add("/pops", content_type="text/html", data=(
        f'<html><body><script src="{other}/js/popunder.js"></script>'
        f'<a id="real" {big} href="/next">Real link</a>'
        f'<div id="blank" {big}>blank pop-up</div>'
        f'<a id="tu" {big} href="/next" target="_blank">Download</a>'
        "<script>"
        f"document.getElementById('real').addEventListener('mousedown', function(){{window.open('{other}/ad')}}, true);"
        "document.getElementById('blank').addEventListener('mousedown', function(){"
        f"  var w = window.open(''); setTimeout(function(){{ w.location = '{other}/ad3'; }}, 100); }});"
        "document.getElementById('tu').addEventListener('click', function(){"
        f"  setTimeout(function(){{ location.href = '{other}/landing'; }}, 50); }});"
        "</script></body></html>").encode())
    dlg = _open_browser(win, core, qtbot, server.url("/pops"), "82")
    qtbot.waitUntil(lambda: browser.ADBLOCK.count("127.0.0.1") >= 1, timeout=15000)   # EasyList: popunder.js
    _click_element(qtbot, dlg, "blank")
    qtbot.waitUntil(lambda: "Blocked a pop-up to localhost" in dlg.notice.text.text(), timeout=15000)
    assert len(dlg.tabs) == 1 and not dlg._pending
    dlg.notice.hide()
    qtbot.wait(300)                                                     # let the page move up first
    _click_element(qtbot, dlg, "tu")                                    # real new tab + the old one sent to an ad
    qtbot.waitUntil(lambda: len(dlg.tabs) == 2, timeout=10000)
    qtbot.waitUntil(lambda: "Blocked a redirect to localhost" in dlg.notice.text.text(), timeout=10000)
    assert dlg.tabs[0].url().toString().endswith("/pops")
    dlg.back()
    dlg.notice.hide()
    qtbot.wait(300)
    _click_element(qtbot, dlg, "real")                                  # pop-under on mouse-down
    qtbot.waitUntil(lambda: dlg.current.url().toString().endswith("/next"), timeout=10000)
    qtbot.waitUntil(lambda: "pop-under" in dlg.notice.text.text(), timeout=10000)
    assert all(t.url().host() != "localhost" for t in dlg.tabs)


def test_filter_lists_block_ads_but_not_downloads_or_checks():
    from ezgd.net import filterlists
    assert filterlists.AVAILABLE and filterlists.info()["rules"] > 100_000
    page = "https://datanodes.to/abc/Game.part1.rar"
    assert filterlists.blocked("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js", page, "script")
    assert filterlists.blocked("https://www.googletagmanager.com/gtm.js", page, "script")
    assert filterlists.popup_blocked("https://www.highperformanceformat.com/x", page)
    for ok in ("https://challenges.cloudflare.com/turnstile/v0/api.js", "https://www.google.com/recaptcha/api.js",
               "https://js.hcaptcha.com/1/api.js", "https://store1.gofile.io/download/web/a/Game.rar"):
        assert not filterlists.blocked(ok, page, "script"), ok
    assert not filterlists.popup_blocked("https://datanodes.to/download", page)
    sel = filterlists.generic_selectors(["sponsored-ad", "content"], ["ad_banner", "main"], set())
    assert ".sponsored-ad" in sel and "#ad_banner" in sel and ".content" not in sel
    js = filterlists.style_js(["#ad_banner", ".sponsored-ad"], "generic")
    assert "#ad_banner{display:none!important}" in js and "data-ezgd" in js


def test_in_app_browser_hides_ad_elements(ui, qtbot, server, monkeypatch):
    from ezgd.net import filterlists
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    # the lists switch element hiding off for localhost, so the rules are stood in for here
    monkeypatch.setattr(filterlists, "cosmetic", lambda url: (["#site-ad"], set(), False))
    monkeypatch.setattr(filterlists, "generic_selectors",
                        lambda c, i, exc: [f"#{x}" for x in i if x == "late-ad"] + [f".{x}" for x in c if x == "ad-box"])
    server.add("/cosm", content_type="text/html", data=(
        "<html><body><div id='site-ad'>site ad</div><div class='ad-box'>generic ad</div><p id='keep'>text</p>"
        "<script>setTimeout(function(){var d=document.createElement('div');d.id='late-ad';d.textContent='late';"
        "document.body.appendChild(d);}, 300);</script></body></html>").encode())
    dlg = _open_browser(win, core, qtbot, server.url("/cosm"), "83")
    shown = {}

    def check():
        dlg.current.runJavaScript(
            "['site-ad','keep','late-ad'].map(function(i){var e=document.getElementById(i);"
            "return e ? getComputedStyle(e).display : 'none';}).join(',') + ',' + "
            "getComputedStyle(document.querySelector('.ad-box')).display", 0, lambda r: shown.update(v=r))
        return shown.get("v") == "none,block,none,none"
    qtbot.waitUntil(check, timeout=15000)


def test_in_app_browser_disguised_download_buttons_still_download(ui, qtbot, server):
    """File hosts' buttons are often <a href="#"> or javascript: links that send you to the file on a CDN by
    script — that is the download, not a 'tab-under' (the redirect check must not block it)."""
    import io
    import zipfile
    from ezgd.ui.dialogs import browser
    if not browser.AVAILABLE:
        pytest.skip("Qt WebEngine not installed")
    win, core, _ = ui
    cdn = server.base.replace("127.0.0.1", "localhost")                 # another site, like a host's CDN
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Btn Game/Game.exe", b"MZ" + b"\0" * 100)
    server.add("/cdn/Btn.Game.zip", data=buf.getvalue(), content_type="application/zip",
               headers={"Content-Disposition": 'attachment; filename="Btn.Game.zip"'})
    big = 'style="display:block;width:400px;height:60px;font-size:30px"'
    server.add("/btn", content_type="text/html", data=(
        f'<html><body><a id="dl" {big} href="#">Download</a><script>'
        "document.getElementById('dl').addEventListener('click', function(e){ e.preventDefault();"
        f"  setTimeout(function(){{ location.href = '{cdn}/cdn/Btn.Game.zip'; }}, 50); }});"
        "</script></body></html>").encode())
    dlg = _open_browser(win, core, qtbot, server.url("/btn"), "84")
    _click_element(qtbot, dlg, "dl")
    qtbot.waitUntil(lambda: bool(dlg.started), timeout=15000)
    assert "Blocked a redirect" not in dlg.notice.text.text()


def test_long_toast_is_not_clipped_and_is_kept(qtbot):
    from PySide6.QtWidgets import QWidget
    from ezgd.ui.dialogs.misc import NotificationsDialog
    from ezgd.ui.widgets.common import Toast, ToastHost
    w = QWidget()
    w.resize(900, 700)
    qtbot.addWidget(w)
    long = "The fix is in the Steam game's folder; replaced files are kept. " * 8
    short = Toast("Done", "ok", "info", w)
    big = Toast("Extraction finished", long, "info", w, action=("Open Library", lambda: None))
    assert big.height() > short.height() + 60          # grows with its text instead of cutting it off
    host = ToastHost(w)
    host.show("Extraction finished", long)
    assert ToastHost.history[-1]["text"] == long
    dlg = NotificationsDialog(ToastHost.history, w)
    qtbot.addWidget(dlg)
    from PySide6.QtWidgets import QPlainTextEdit
    assert long.strip() in dlg.findChild(QPlainTextEdit).toPlainText()


def test_all_games_keeps_its_order_while_more_results_arrive():
    """Scrolling loaded more results and re-sorted everything — the games under the mouse moved (2026-10-13)."""
    from types import SimpleNamespace
    from ezgd.catalog.models import Game
    from ezgd.catalog.search import Filters, SortKey
    from ezgd.ui.pages.all_games import AllGamesPage
    shown = []
    page = SimpleNamespace(
        games={}, loading=False, errors={}, actions=SimpleNamespace(installed_keys=set()),
        search=SimpleNamespace(text=lambda: ""), filters=lambda: Filters(), sort_key=lambda: SortKey.RECENT,
        grid=SimpleNamespace(set_installed=lambda k: None, set_groups=lambda g: shown.append([x.title for x in g])),
        stack=SimpleNamespace(setCurrentWidget=lambda w: None), empty=None, _update_status=lambda: None)
    for i, t in enumerate(["B game", "D game"]):
        page.games[t] = Game("src", t, t, f"https://x/{i}", published_at=100 + i * 10)
    AllGamesPage._refilter(page)
    first = shown[-1]
    page.games["N game"] = Game("src", "N", "N game", "https://x/n", published_at=999)     # newest: would sort first
    AllGamesPage._refilter(page, keep_order=True)
    assert shown[-1][:2] == first and shown[-1][-1] == "N game"                       # appended, nothing moved
    AllGamesPage._refilter(page)                                                      # e.g. the sort changed
    assert shown[-1][0] == "N game"
