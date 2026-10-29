"""Desktop quality of life: drag and drop, clipboard links, remembered view, shortcuts, tray/taskbar texts."""

from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import QApplication

from test_ui import ui  # noqa: F401 — the shared main-window fixture

from ezgd.ui import dropin, taskbar

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Test"


def _mime(urls=(), text=None) -> QMimeData:
    m = QMimeData()
    if urls:
        m.setUrls([QUrl(u) if "://" in u or u.startswith("magnet:") else QUrl.fromLocalFile(u) for u in urls])
    if text is not None:
        m.setText(text)
    return m


def test_classify_drops(tmp_path, qapp):
    tor = tmp_path / "game.torrent"
    tor.write_bytes(b"d4:infod4:name1:xee")
    folder = tmp_path / "Some Game"
    folder.mkdir()
    parts = []
    for n in (1, 2, 3):
        p = tmp_path / f"Big.Game-OFME.part{n}.rar"
        p.write_bytes(b"x")
        parts.append(str(p))
    single = tmp_path / "Small.Game.v1.2.zip"
    single.write_bytes(b"x")
    junk = tmp_path / "notes.txt"
    junk.write_text("hi")
    got = dropin.classify(_mime([str(tor), str(folder), parts[2], parts[0], parts[1], str(single), str(junk),
                                 "https://example.com/file.zip", MAGNET]))
    assert ("torrent", str(tor)) in got
    assert ("import", str(folder)) in got
    assert [v for k, v in got if k == "import" and "Big.Game" in v] == [parts[0]]    # a set once, first part
    assert ("import", str(single)) in got
    assert not any(str(junk) == v for _k, v in got)
    assert ("link", "https://example.com/file.zip") in got and ("link", MAGNET) in got
    assert dropin.classify(_mime(text=f"{MAGNET}\nnot a link")) == [("link", MAGNET)]
    assert dropin.classify(_mime(text="just words")) == []


def test_guess_title():
    g = dropin.guess_title
    assert g("How.to.Fish.v1.0.10-OFME.rar") == "How to Fish"
    assert g("Valor_Mortis-SteamRIP.com.part01.rar") == "Valor Mortis"
    assert g("Slay the Spire 2 [FitGirl Repack].7z") == "Slay the Spire 2"
    assert g("Hollow.Knight.Build.1234.zip.001") == "Hollow Knight"
    assert g("Hades II (v1.0) [DODI].rar") == "Hades II"
    assert g("Plain Name") == "Plain Name"


def test_clipboard_magnet_is_offered_once(ui, qtbot, monkeypatch):  # noqa: F811
    win, _core, _ = ui
    QGuiApplication.clipboard().setText(MAGNET)
    shown = []
    monkeypatch.setattr(win.toasts, "show", lambda *a, **k: shown.append((a, k)))
    win._offer_clipboard()
    assert len(shown) == 1 and shown[0][1]["action"][0] == "Add download"
    win._offer_clipboard()                                  # the same link again: not offered twice
    assert len(shown) == 1
    QGuiApplication.clipboard().setText("https://example.com/page")     # an ordinary web address: never
    win._offer_clipboard()
    assert len(shown) == 1


def test_dropped_items_go_to_the_right_place(ui, qtbot, monkeypatch, tmp_path):  # noqa: F811
    win, _core, _ = ui
    calls = []
    monkeypatch.setattr(win.actions, "add_download_dialog", lambda parent, initial="", torrent="":
                        calls.append(("add", initial, torrent)))
    monkeypatch.setattr(win.pages["library"], "import_path", lambda p: calls.append(("import", p)))
    win._take_dropped([("link", MAGNET), ("torrent", "/x/g.torrent"), ("import", str(tmp_path))])
    assert calls == [("add", MAGNET, ""), ("add", "", "/x/g.torrent"), ("import", str(tmp_path))]
    assert win.stack.currentWidget() is win.pages["library"]


def test_import_path_suggests_a_clean_title(ui, qtbot, monkeypatch, tmp_path):  # noqa: F811
    win, _core, _ = ui
    asked = {}

    def fake_get_text(_parent, _title, _label, text=""):
        asked["text"] = text
        return "", False
    from ezgd.ui.pages import library
    monkeypatch.setattr(library.QInputDialog, "getText", staticmethod(fake_get_text))
    win.pages["library"].import_path(str(tmp_path / "How.to.Fish.v1.0.10-OFME.rar"))
    assert asked["text"] == "How to Fish"


def test_escape_clears_the_search_box(ui, qtbot):  # noqa: F811
    win, _core, _ = ui
    win.navigate("library")
    box = win.pages["library"].filter
    box.setText("abc")
    QApplication.sendEvent(box, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert box.text() == ""


def test_view_is_remembered(ui, qtbot):  # noqa: F811
    win, _core, _ = ui
    games = win.pages["games"]
    win.navigate("downloads")
    games.sort.setCurrentIndex(2)
    next(iter(games.source_buttons.values())).setChecked(False)
    win._save_view()
    st = games.view_state()
    games.sort.setCurrentIndex(0)
    for b in games.source_buttons.values():
        b.setChecked(True)
    win.navigate("home")
    win._restore_view()
    assert win.stack.currentWidget() is win.pages["downloads"]
    assert games.sort.currentIndex() == 2 and games.view_state()["off"] == st["off"]


def test_wishlist_badge_and_shortcut_list(ui, qtbot):  # noqa: F811
    win, _core, _ = ui
    win.pages["wishlist"].available_changed.emit(2)
    assert "2 available" in win.nav_buttons["wishlist"].text()
    win.pages["wishlist"].available_changed.emit(0)
    assert win.nav_buttons["wishlist"].text().strip() == "Wishlist"
    assert any(k == "Ctrl+N" for k, _ in __import__("ezgd.ui.main_window", fromlist=["SHORTCUTS"]).SHORTCUTS)


def test_tray_texts():
    assert taskbar.eta_text(None) == "" and taskbar.eta_text(30) == "less than a minute"
    assert taskbar.eta_text(125) == "2 min" and taskbar.eta_text(3 * 3600 + 5 * 60) == "3 h 05 min"
    assert taskbar.tray_tooltip("EZ", {"active": 0, "queued": 0}) == "EZ"
    t = taskbar.tray_tooltip("EZ", {"active": 1, "queued": 1, "speed": 2 * 1024 * 1024, "progress": 0.42,
                                    "eta": 600})
    assert "2 downloads" in t and "42 %" in t and "10 min left" in t


def test_find_a_setting(ui, qtbot):  # noqa: F811
    win, _core, _ = ui
    win.navigate("settings")
    page = win.pages["settings"]
    page.find.setText("proton")
    shown = [c for c, _h, _f in page._sections if c.isVisibleTo(page)]
    assert shown and len(shown) < len(page._sections)
    page.find.setText("qqqzzzx")
    assert not any(c.isVisibleTo(page) for c, _h, _f in page._sections) and page.no_match.isVisibleTo(page)
    page.find.clear()
    assert all(c.isVisibleTo(page) for c, _h, _f in page._sections) and not page.no_match.isVisibleTo(page)


def test_closing_the_window_ends_the_app_whatever_couch_mode_left(ui, qtbot, monkeypatch):  # noqa: F811
    """A friend's app stayed in memory (1.3 GB) after its window was closed (2026-10-29): couch mode and the quick menu
    over a desktop-started game switch Qt's "quit with the last window" off while they are up, and with it left off
    the window's ✕ only hid the window — no window, no tray icon, the app still running. Closing the window (nothing
    asked to keep it in the tray) ends the app, whatever that rule says."""
    win, _core, _ = ui
    quits = []
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: quits.append(1))
    QApplication.instance().setQuitOnLastWindowClosed(False)
    win.close()
    assert quits == [1] and not win.isVisible()
    assert QApplication.instance().quitOnLastWindowClosed()


def test_quitting_from_the_tray_ends_the_app(ui, qtbot, monkeypatch):  # noqa: F811
    """Started with --minimized (or hidden in the tray), SIGTERM's force_quit must still end Qt's loop."""
    win, _core, _ = ui
    win.hide()
    quits = []
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: quits.append(1))
    win.force_quit()
    assert quits == [1]
