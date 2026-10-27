"""Main window: sidebar navigation, pages, event routing, notifications, shutdown."""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from ezgd import APP_DISPLAY_NAME
from ezgd.core.fsutil import format_speed
from ezgd.ui import theme
from ezgd.ui.actions import Actions
from ezgd.ui.icons import app_icon, icon
from ezgd.ui.pages.all_games import AllGamesPage
from ezgd.ui.pages.downloads import DownloadsPage
from ezgd.ui.pages.home import HomePage
from ezgd.ui.pages.library import LibraryPage
from ezgd.ui.pages.picks import PicksPage
from ezgd.ui.pages.settings import SettingsPage
from ezgd.ui.pages.wishlist import WishlistPage
from ezgd.ui.widgets.common import ToastHost, label
from ezgd.ui.widgets.covers import CoverCache
from ezgd.ui.dialogs import browser as _browser    # loaded up front: quitting must not import
from ezgd.ui import dropin, taskbar

log = logging.getLogger(__name__)

NAV = [("home", "Home", "home"), ("games", "All Games", "grid"), ("picks", "OnlineFix Picks", "star"),
       ("wishlist", "Wishlist", "heart"), ("downloads", "Downloads", "download"), ("library", "Library", "library"),
       ("settings", "Settings", "settings")]
THEME_KEYS = {"theme_accent", "theme_contrast"}
SHORTCUTS = [("Ctrl+F", "Search (the page's own search, else All Games)"), ("Ctrl+1 … Ctrl+7", "Go to a page"),
             ("Ctrl+N", "Add a download (magnet link, .torrent, file link)"), ("Ctrl+,", "Settings"),
             ("F5", "Refresh the page"), ("F11", "Couch mode"), ("Esc", "Clear the search box you are typing in"),
             ("Enter (Library list)", "Play the selected game"), ("F1", "This list"),
             ("Drag and drop", "A .torrent, a magnet / download link, a game folder or an archive onto the window")]
REMEMBERED_PAGES = ("home", "games", "picks", "wishlist", "downloads", "library")


class MainWindow(QMainWindow):
    def __init__(self, bridge, paths, settings: dict):
        super().__init__()
        self.bridge = bridge
        self.paths = paths
        self.settings = settings
        self._quitting = False
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setWindowIcon(app_icon())
        QTimer.singleShot(2000, self, self._register_launcher)     # (pinnable to the taskbar, same icon closed)
        self.resize(1360, 860)
        self.setMinimumSize(1020, 640)
        self._ui_state = QSettings(str(paths.data / "window.ini"), QSettings.IniFormat)   # size/position
        geo = self._ui_state.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        QShortcut(QKeySequence.Find, self, self._focus_search)                  # Ctrl+F
        for n, (key, _t, _i) in enumerate(NAV, 1):                               # Ctrl+1 … Ctrl+7
            QShortcut(QKeySequence(f"Ctrl+{n}"), self, lambda k=key: self.navigate(k))
        QShortcut(QKeySequence(Qt.Key_F11), self, self.open_couch)               # couch mode
        QShortcut(QKeySequence("Ctrl+N"), self, lambda: self.actions.add_download_dialog(self))
        QShortcut(QKeySequence("Ctrl+,"), self, lambda: self.navigate("settings"))
        QShortcut(QKeySequence(Qt.Key_F5), self, self.refresh_page)
        QShortcut(QKeySequence(Qt.Key_F1), self, self.show_shortcuts)
        self.couch = None
        self._couch_gpu_failed = False            # couch mode's GPU drawing failed this run: draw it with the CPU
        self._quick_pads = None                   # the guide button while a game runs and couch mode is closed
        self._quick_key = None                    # Shift+Tab over one of our games (hotkey.py), while one runs
        self._bpm_timer = QTimer(self)            # … and Steam's Big Picture taking it (Steam Input)
        self._bpm_timer.setInterval(1500)
        self._bpm_timer.timeout.connect(self._bpm_tick)
        self._clip_seen: set[str] = set()                # clipboard links already offered
        self._tray_hint_shown = False
        self.setAcceptDrops(True)

        self.toasts = ToastHost(self)
        ToastHost.history_changed = lambda: QTimer.singleShot(0, self._notifications_changed)
        self._syscheck_timer = QTimer(self)               # Home's "setup is incomplete" banner stays current
        self._syscheck_timer.setInterval(120_000)
        self._syscheck_timer.timeout.connect(lambda: self.pages["home"].check_system() if hasattr(self, "pages") else None)
        self._syscheck_timer.start()
        self.toasts.reduce_motion = bool(settings.get("reduce_motion"))
        self.covers = CoverCache(bridge, self)
        self.actions = Actions(bridge, self.covers, self.toasts, settings, self)
        self.actions.on_launch = self._launch_requested   # a game started again is never "the one to close"
        self.actions.navigate.connect(self.navigate)
        bridge.error.connect(lambda msg: self.toasts.show("Something went wrong", msg, "error"))

        root = QWidget()
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self._sidebar())
        self.stack = QStackedWidget()
        rl.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self._drop_hint = dropin.DropHint(self)

        try:                                      # user-added sites need names/colours before pages exist
            theme.register_sources(bridge.run_sync(bridge.core.get_sources(), 10))
        except Exception:
            log.exception("could not load the source list")
        self.pages = {
            "home": HomePage(self.actions), "games": AllGamesPage(self.actions), "picks": PicksPage(self.actions),
            "wishlist": WishlistPage(self.actions), "downloads": DownloadsPage(self.actions),
            "library": LibraryPage(self.actions),
            "settings": SettingsPage(self.actions, paths),
        }
        for p in self.pages.values():
            self.stack.addWidget(p)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip(APP_DISPLAY_NAME)
            self.tray.activated.connect(self._tray_activated)
            self.tray.setContextMenu(taskbar.tray_menu(self))
            self.tray.show()
        self.launcher = taskbar.LauncherEntry()            # progress on the taskbar icon

        bridge.event.connect(self._on_event)
        self._library_timer = QTimer(self)
        self._library_timer.setSingleShot(True)
        self._library_timer.setInterval(300)
        self._library_timer.timeout.connect(self._reload_library)
        self._wishlist_timer = QTimer(self)
        self._wishlist_timer.setSingleShot(True)
        self._wishlist_timer.setInterval(300)
        self._wishlist_timer.timeout.connect(self._wishlist_changed)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._poll_summary)
        self._status_timer.start()

        self._apply_settings_to_pages()
        self.pages["downloads"].load_all()
        self.pages["library"].load_environment()
        self._reload_library()
        self.pages["home"].load()
        self.pages["wishlist"].available_changed.connect(self._wishlist_badge)
        self.pages["wishlist"].load()
        for box in (self.pages["home"].search, self.pages["games"].search, self.pages["library"].filter,
                    self.pages["settings"].find):
            box.installEventFilter(self)                   # Esc clears it
        self._restore_view()

    # -------------------------------------------------------------- layout
    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(232)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 18, 14, 14)
        lay.setSpacing(4)
        logo = QHBoxLayout()
        ic = label()
        ic.setPixmap(app_icon().pixmap(30, 30))
        logo.addWidget(ic)
        logo.addWidget(label("EZ Games", "logo"), 1)
        lay.addLayout(logo)
        lay.addSpacing(18)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons: dict[str, QPushButton] = {}
        for key, text, ic_name in NAV:
            b = QPushButton(f"  {text}")
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setIcon(icon(ic_name, theme.CURRENT.muted))
            b.setIconSize(QSize(20, 20))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.navigate(k))
            self.nav_group.addButton(b)
            self.nav_buttons[key] = b
            lay.addWidget(b)
        lay.addStretch(1)
        self.couch_btn = QPushButton("  Couch mode")
        self.couch_btn.setObjectName("couch")
        self.couch_btn.setIcon(icon("gamepad", "#ffffff"))
        self.couch_btn.setIconSize(QSize(22, 22))
        self.couch_btn.setCursor(Qt.PointingHandCursor)
        self.couch_btn.setToolTip("Full-screen library for the TV or a controller, like Steam's Big Picture (F11). "
                                  "The ✕ in its top-right corner brings you back.")
        self.couch_btn.clicked.connect(lambda _=False: self.open_couch())
        lay.addWidget(self.couch_btn)
        lay.addSpacing(6)
        self.bell = QPushButton("  Notifications")
        self.bell.setObjectName("nav")
        self.bell.setIcon(icon("info", theme.CURRENT.muted))
        self.bell.setIconSize(QSize(20, 20))
        self.bell.setCursor(Qt.PointingHandCursor)
        self.bell.setToolTip("Every message of this session in full (pop-ups and desktop notifications can be short)")
        self.bell.clicked.connect(self._show_notifications)
        lay.addWidget(self.bell)
        self.side_status = label("", "small", wrap=True)
        lay.addWidget(self.side_status)
        return side

    def changeEvent(self, e) -> None:
        """Back in the app: re-check the system tools (something may have been installed meanwhile)."""
        from PySide6.QtCore import QEvent
        import time as _t
        if e.type() == QEvent.ActivationChange and self.isActiveWindow() and \
                _t.monotonic() - getattr(self, "_last_syscheck", 0) > 20:
            self._last_syscheck = _t.monotonic()
            try:
                self.pages["home"].check_system()
            except (KeyError, RuntimeError, AttributeError):
                pass
        if e.type() == QEvent.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(150, self, self._offer_clipboard)     # (the clipboard is readable once active)
        super().changeEvent(e)

    # ------------------------------------------------------- getting things in
    def _offer_clipboard(self) -> None:
        """A magnet / .torrent link copied in the browser: offer to add it (once per link)."""
        if self._quitting or self.couch is not None or QApplication.activeModalWidget() is not None:
            return
        try:
            link = dropin.clipboard_link()
        except RuntimeError:
            return
        if not link:
            return
        key = dropin.link_key(link)
        if key in self._clip_seen:
            return
        self._clip_seen.add(key)
        what = "A magnet link" if link.startswith("magnet:") else "A link to a .torrent file"
        self.toasts.show(f"{what} is on your clipboard", "Add it as a download?", "info",
                         action=("Add download", lambda: self.actions.add_download_dialog(self, link)))

    def dragEnterEvent(self, e) -> None:                    # noqa: N802
        items = dropin.classify(e.mimeData())
        if items and self.couch is None:
            e.acceptProposedAction()
            self._drop_hint.show_for(items)
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:                     # noqa: N802
        e.acceptProposedAction() if self._drop_hint.isVisible() else e.ignore()

    def dragLeaveEvent(self, e) -> None:                    # noqa: N802
        self._drop_hint.hide()
        super().dragLeaveEvent(e)

    def dropEvent(self, e) -> None:                         # noqa: N802
        self._drop_hint.hide()
        items = dropin.classify(e.mimeData())
        if not items:
            e.ignore()
            return
        e.acceptProposedAction()
        # one after the other, after the drop has finished (dialogs inside a drop handler block the source app)
        QTimer.singleShot(0, self, lambda: self._take_dropped(items))

    def _take_dropped(self, items: list[tuple[str, str]]) -> None:
        self.raise_()
        self.activateWindow()
        for kind, value in items:
            if kind == "link":
                self._clip_seen.add(dropin.link_key(value))
                self.actions.add_download_dialog(self, value)
            elif kind == "torrent":
                self.actions.add_download_dialog(self, torrent=value)
            elif kind == "import":
                self.navigate("library")
                self.pages["library"].import_path(value)

    def eventFilter(self, obj, e) -> bool:                  # noqa: N802
        if e.type() == QEvent.KeyPress and e.key() == Qt.Key_Escape and isinstance(obj, QLineEdit) and obj.text():
            obj.clear()                                     # Esc clears a search box
            return True
        return super().eventFilter(obj, e)

    def where(self) -> str:
        """What is on screen, for the log's UI stall lines (see stalls.py)."""
        couch = self.couch
        if couch is not None and couch.isVisible():
            from ezgd.ui.couch import TABS
            tab = TABS[couch.tab] if 0 <= getattr(couch, "tab", -1) < len(TABS) else "?"
            return f"couch mode ({tab}{', a page over it' if getattr(couch, 'stack', None) else ''})"
        key = next((k for k, p in self.pages.items() if p is self.stack.currentWidget()), "?")
        return f"the {key} page" + ("" if self.isVisible() else " (window hidden)")

    def refresh_page(self) -> None:
        key = next((k for k, p in self.pages.items() if p is self.stack.currentWidget()), "")
        page = self.pages.get(key)
        if key == "home":
            page.load()
        elif key == "games":
            page.reload()
        elif key == "picks":
            page.load(force=True)
        elif key == "wishlist":
            page.load()
        elif key == "downloads":
            page.load_all()
        elif key == "library":
            self._reload_library()
        elif key == "settings":
            self.navigate("settings")

    def show_shortcuts(self) -> None:
        rows = "".join(f"<tr><td style='padding:3px 18px 3px 0'><b>{k}</b></td><td>{v}</td></tr>" for k, v in SHORTCUTS)
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard shortcuts")
        box.setTextFormat(Qt.RichText)
        box.setText(f"<table>{rows}</table>")
        box.exec()

    def _wishlist_badge(self, n: int) -> None:
        try:
            self.nav_buttons["wishlist"].setText(f"  Wishlist ({n} available)" if n else "  Wishlist")
        except RuntimeError:
            pass

    # ------------------------------------------------------- remembered view
    def _restore_view(self) -> None:
        import json
        page = self._ui_state.value("page", "home")
        try:
            games = json.loads(self._ui_state.value("games_view", "{}") or "{}")
            if isinstance(games, dict):
                self.pages["games"].restore_view(games)
        except (ValueError, TypeError):
            pass
        try:
            sel = int(self._ui_state.value("library_selected", 0) or 0)
        except (ValueError, TypeError):
            sel = 0
        if sel and self.pages["library"].current_id is None:
            self.pages["library"].current_id = sel
        lib_sort = self.pages["library"].sort
        lib_sort.blockSignals(True)
        lib_sort.setCurrentIndex(max(0, lib_sort.findData(self._ui_state.value("library_sort", "recent"))))
        lib_sort.blockSignals(False)
        self.navigate(page if page in REMEMBERED_PAGES else "home")

    def _save_view(self) -> None:
        import json
        key = next((k for k, p in self.pages.items() if p is self.stack.currentWidget()), "home")
        self._ui_state.setValue("page", key)
        self._ui_state.setValue("games_view", json.dumps(self.pages["games"].view_state()))
        self._ui_state.setValue("library_selected", self.pages["library"].current_id or 0)
        self._ui_state.setValue("library_sort", self.pages["library"].sort.currentData() or "recent")

    # ------------------------------------------------------------- the tray
    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
                self.hide()
            else:
                self.show_from_tray()

    def show_from_tray(self) -> None:
        if self.couch is not None:
            self.couch.open(self.couch.screen())
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self) -> None:
        self.show_from_tray()
        self.close()

    def _notifications_changed(self) -> None:
        from ezgd.ui.widgets.common import ToastHost
        n = sum(1 for h in ToastHost.history if not h.get("seen"))
        try:
            self.bell.setText(f"  Notifications ({n})" if n else "  Notifications")
        except RuntimeError:
            pass

    def _show_notifications(self) -> None:
        from ezgd.ui.dialogs.misc import NotificationsDialog
        from ezgd.ui.widgets.common import ToastHost
        NotificationsDialog(ToastHost.history, self).exec()
        for h in ToastHost.history:
            h["seen"] = True
        self._notifications_changed()

    def navigate(self, key: str) -> None:
        if key == "refresh-environment":            # after installing system tools
            self.pages["library"].load_environment()
            self.pages["home"].check_system()
            return
        if key == "system-check":
            from ezgd.ui.dialogs.system import SystemCheckDialog
            SystemCheckDialog(self.actions, self).exec()
            return
        query = None
        if key.startswith("games:"):
            key, query = "games", key.split(":", 1)[1]
        page = self.pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
            b.setIcon(icon(dict((n[0], n[2]) for n in NAV)[k], "#ffffff" if k == key else theme.CURRENT.muted))
        if query is not None:
            page.set_query(query)
        if key == "settings":
            page.load(self.settings)                 # instant, then refreshed from the core
            self.bridge.call(self.bridge.core.get_settings(), page.load, owner=page)
        if key == "library":
            self._reload_library()

    # --------------------------------------------------------------- events
    def _on_event(self, name: str, payload) -> None:
        if self._quitting:
            return
        pages = self.pages
        if name == "download_updated":
            pages["downloads"].upsert(payload)
            pages["home"].download_updated(payload)
        elif name == "download_removed":
            pages["downloads"].removed(payload)
            pages["home"].download_removed(payload)
        elif name == "download_completed":
            self._library_timer.start()
        elif name == "extraction":
            pages["downloads"].extraction(payload)
            pages["library"].extraction(payload)
            if self.couch is not None:
                self.couch.extraction_state(payload)
        elif name == "sources_changed":
            def apply(sources):
                theme.register_sources(sources)
                self.pages["games"].sources_changed()
                self.pages["settings"].load_custom_sources(sources)
                self.pages["home"].load_sources()
            self.bridge.call(self.bridge.core.get_sources(), apply)
        elif name == "games_hidden":
            ids = set(payload)
            pages["games"].hide_ids(ids)
            pages["picks"].hide_ids(ids)
            pages["home"].hide_ids(ids)
        elif name == "fixmygame":
            if self.couch is not None:
                self.couch.fix_progress(payload)
        elif name == "game_state":
            pages["library"].game_state(payload)
            self._close_if_pending(payload)
            if self.couch is not None:
                self.couch.game_state(payload)
            self._watch_quick_button()
            running = len(pages["library"].running)
            self.nav_buttons["library"].setText(f"  Library  ● {running} playing" if running else "  Library")
        elif name == "library_changed":
            self._library_timer.start()
        elif name == "notify":
            title, text, *extra = payload
            extra = extra[0] if extra and isinstance(extra[0], dict) else {}
            low = title.lower()
            kind = "error" if "fail" in low else "success" if any(w in low for w in (
                "finish", "ready", "installed", "worked")) else "info"
            action = None
            if title in ("Ready to play", "Game installed", "Ready to install", "Extraction finished"):
                action = ("Open Library", lambda: self.navigate("library"))
            if extra.get("diagnose") or title == "Still not working":
                kind = "warning"
                eid = extra.get("diagnose")
                if eid:
                    action = ("Open Diagnose", lambda eid=eid: self._open_diagnose(eid))
            if extra.get("library"):
                action = ("Open in Library", lambda eid=extra["library"]: self.open_library_entry(eid))
            elif extra.get("wishlist"):
                action = ("Open Wishlist", lambda: self.navigate("wishlist"))
            elif extra.get("app_update"):
                action = ("Open Settings", lambda: self.navigate("settings"))
            if self.couch is not None:
                action = self._couch_action(extra, title)          # buttons that open couch mode's own pages
            host = self.couch.toasts if self.couch is not None else self.toasts
            host.show(title, text, kind, *([action] if action else []))
            window_active = self.isActiveWindow() or (self.couch is not None and self.couch.isActiveWindow())
            if self.tray and self.settings.get("notifications_enabled", True) and not window_active:
                # desktop notifications cut long text: a short version, the full one is in Notifications
                short = text if len(text) <= 160 else text[:150].rsplit(" ", 1)[0] + "… (details in the app)"
                self.tray.showMessage(title, short, QSystemTrayIcon.Critical if kind == "error"
                                      else QSystemTrayIcon.Information, 8000)
        elif name == "settings_changed":
            self.bridge.call(self.bridge.core.get_settings(), lambda s: self._settings_loaded(s, payload), owner=self)
        elif name == "wishlist_changed":
            self._wishlist_timer.start()
        elif name == "saves_changed":
            pages["library"].saves_changed(payload)
        elif name in ("app_update", "app_update_progress"):
            pages["settings"].app_update_event(name, payload)

    def _wishlist_changed(self) -> None:
        self.pages["wishlist"].load()
        if self.couch is not None:
            self.couch.wishlist_changed()

    def _couch_action(self, extra: dict, title: str):
        """A message's button inside couch mode: it opens couch mode's page, not the hidden desktop window."""
        from ezgd.ui import couch as c
        if extra.get("library") or extra.get("diagnose"):
            eid = extra.get("library") or extra.get("diagnose")
            return ("Show in Library", lambda: self.couch and self.couch.go_to(c.T_LIBRARY, eid))
        if title in ("Ready to play", "Game installed", "Ready to install", "Extraction finished"):
            return ("Show in Library", lambda: self.couch and self.couch.go_to(c.T_LIBRARY))
        if extra.get("wishlist"):
            return ("Open Wishlist", lambda: self.couch and self.couch.go_to(c.T_WISHLIST))
        if extra.get("app_update"):
            return ("Open Settings", lambda: self.couch and self.couch.go_to(c.T_SETTINGS))
        return None

    def _register_launcher(self) -> None:
        """The launcher entry that lets the app be pinned to the taskbar, with the icon this window shows (see
        platform.appentry). Not for test runs without a real screen."""
        if QApplication.platformName() in ("offscreen", "minimal"):
            return
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from ezgd.platform import appentry
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        app_icon().pixmap(256, 256).save(buf, "PNG")
        buf.close()
        appentry.register(bytes(data), str(self.settings.get("theme_accent") or "violet"))

    def _settings_loaded(self, s: dict, changed_key: str) -> None:
        self.settings.clear()
        self.settings.update(s)
        if changed_key in THEME_KEYS:
            theme.apply_theme(QApplication.instance(), s["theme_accent"], s["theme_contrast"])
            self.setWindowIcon(app_icon())                    # (drawn in the accent colour)
            if getattr(self, "tray", None) is not None:
                self.tray.setIcon(app_icon())
            self._register_launcher()
            self.navigate(next(k for k, b in self.nav_buttons.items() if b.isChecked()))
            self.toasts.show("Theme updated", "Some colours update fully after a restart.")
        self._apply_settings_to_pages()
        self.pages["settings"].load(s, info=False)        # a change made in couch mode shows here too
        if self.couch is not None:
            self.couch.settings_changed(changed_key)      # … and one made here, in couch mode
        if changed_key in ("sources_enabled", "hide_withdrawn_games", "hide_adult_games"):
            self.pages["games"].reload()
            self.pages["home"].load_sources()

    def _apply_settings_to_pages(self) -> None:
        s = self.settings
        for key in ("home", "games", "picks"):
            self.pages[key].apply_card_size(s.get("card_size", "comfortable"))
        self.pages["games"].set_enabled_sources(s.get("sources_enabled", {}))
        self.pages["downloads"].load_settings(s)
        self.toasts.reduce_motion = bool(s.get("reduce_motion"))

    def _reload_library(self) -> None:
        def loaded(entries):
            self.pages["library"]._loaded(entries)
            for key in ("home", "games", "picks"):
                self.pages[key].refresh_installed()
            if self.couch is not None:
                self.couch.set_entries(entries)
        self.bridge.call(self.bridge.core.library_entries(), loaded, owner=self)

    def _poll_summary(self) -> None:
        def show(s: dict) -> None:
            if not s["active"] and not s["queued"]:
                text = "No downloads running"
            else:
                speed = format_speed(s["speed"]) if s["speed"] else "starting…" if s["active"] else "waiting"
                text = f"↓ {speed}  ·  {s['active']} active, {s['queued']} queued"
            if s.get("waits_until"):
                text += f"\n⏾ Downloads wait until {s['waits_until']}"
            elif s.get("limited_for_game"):
                text += "\n🎮 Slower while you play"
            if s.get("torrent_error"):
                text += f"\n⚠ {s['torrent_error']}"
            self.side_status.setText(text)
            self.pages["downloads"].set_summary(s)
            n = s["active"] + s["queued"]
            self.nav_buttons["downloads"].setText(f"  Downloads ({n})" if n else "  Downloads")
            self.launcher.update(s.get("progress"), n)
            if self.tray:
                self.tray.setToolTip(taskbar.tray_tooltip(APP_DISPLAY_NAME, s))
        self.bridge.call(self.bridge.core.summary(), show, owner=self)

    # --------------------------------------------------------------- window
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.toasts.relayout()

    def _watch_quick_key(self, playing: bool) -> None:
        """Shift+Tab over one of our games opens the quick menu (keyboard and mouse) — the focus is only followed
        while a game runs, and the key is only taken while that game has the focus (see hotkey.py)."""
        if playing:
            if self._quick_key is None:
                from ezgd.ui.hotkey import QuickMenuKey
                self._quick_key = QuickMenuKey(self._quick_key_wanted, self._quick_menu_by_key, self)
                QApplication.instance().aboutToQuit.connect(self._quick_key.stop)
            self._quick_key.start()
        elif self._quick_key is not None:
            self._quick_key.stop()

    def _quick_key_wanted(self, pid: int, answer) -> None:
        self.bridge.call(self.bridge.core.library_call("quick_menu_key", pid), answer, lambda _e: answer(False),
                         owner=self)

    def _quick_menu_by_key(self) -> None:
        if self.couch is not None:
            self.couch._home_held()                      # (couch mode, or the menu's own background one)
        elif self.pages["library"].running:
            self._quick_menu_from_desktop()

    def force_quit(self) -> None:
        self._quitting = True
        visible = self.isVisible()
        self.close()
        if not visible:
            # in the tray (or never shown, --minimized): closing a hidden window does not end Qt's loop, so a
            # logout / SIGTERM / update left the app running without a window
            QApplication.instance().quit()

    def _open_diagnose(self, entry_id: int) -> None:
        from ezgd.ui.dialogs.diagnose import open_diagnose
        self.navigate("library")

        def got(e) -> None:
            open_diagnose(self.actions, e, self)
        e = self.pages["library"].entries.get(entry_id)
        if e is not None:
            got(e)
        else:
            self.bridge.call(self.bridge.core.library_call("get", entry_id), got, owner=self)

    # ---------------------------------------------------------- couch mode
    def open_couch(self) -> None:
        """Full-screen couch mode on the screen this window is on; this window comes back when it closes."""
        if self.couch is not None:
            if self.couch.background:
                self.couch._promote()                   # the quick menu's hidden couch mode: shown for real
            else:
                self.couch.open(self.couch.screen())
            return
        from ezgd.ui.couch import make_couch
        screen = self.screen()
        try:
            couch = make_couch(self.actions, list(self.pages["library"].entries.values()),
                                set(self.pages["library"].running), gpu_drawing=not self._couch_gpu_failed)
        except Exception:
            log.exception("couch mode could not start")
            self.toasts.show("Couch mode could not start", "The error is in the log.", "error")
            return
        self.couch = couch
        couch.suspended = set(self.pages["library"].suspended)        # (e.g. frozen here on the desktop)
        couch.focus_entry(self.pages["library"].current_id)          # the game chosen here
        self._watch_quick_button()                        # (couch mode listens to the controllers itself)
        couch.closed.connect(self._couch_closed)
        if hasattr(couch, "gpu_failed"):
            couch.gpu_failed.connect(self._couch_gpu_failure)
        self._main_toasts = self.toasts
        self.actions.toasts = couch.toasts                 # messages from game actions show in couch mode
        QApplication.instance().setQuitOnLastWindowClosed(False)
        log.info("couch mode opens from the desktop window: %dx%d%s on %s", self.width(), self.height(),
                 " (maximized)" if self.isMaximized() else " (full screen)" if self.isFullScreen() else "",
                 screen.name() if screen is not None else "?")
        # how the desktop window was, so it comes back that way (a friend's KDE gave it couch mode's full-screen
        # size when it came back: no title bar, over the taskbar)
        self._before_couch = (self.normalGeometry().size() if self.isMaximized() or self.isFullScreen()
                              else self.size(), self.isMaximized(), self.isFullScreen())
        try:
            couch.open(screen)
            self.hide()
        except Exception:
            log.exception("couch mode could not be shown")
            self._couch_closed()

    # ------------------------------------------------- the quick menu over a game started from the desktop
    def _watch_quick_button(self) -> None:
        """While one of our games runs and couch mode is closed: listen for the guide button (tap or hold) to show
        the quick menu over the game. Nothing listens otherwise."""
        self._watch_quick_key(bool(self.pages["library"].running))
        want = bool(self.pages["library"].running) and self.couch is None
        if want and self._quick_pads is None:
            from ezgd.ui.gamepad import Gamepads
            pads = Gamepads(self)
            pads.action.connect(lambda n: self._desktop_guide(pads) if n == "home" else None)
            pads.home_held.connect(self._quick_menu_from_desktop)
            pads.start()
            self._quick_pads = pads
        elif not want and self._quick_pads is not None:
            self._quick_pads.stop()
            self._quick_pads.deleteLater()
            self._quick_pads = None
        from ezgd.ui import steam_bpm
        if want and steam_bpm.steam_running():
            if not self._bpm_timer.isActive():
                self._bpm_timer.start()
        else:
            self._bpm_timer.stop()

    def _desktop_guide(self, pads) -> None:
        """The guide button tapped over a game started from the desktop: the quick menu — unless it was a Steam
        Controller's tap (Steam logged it) and Steam's overlay is in the game: then the tap is Steam's (its overlay:
        friends, invites) and a hold is the quick menu."""
        lib = self.pages["library"]
        ids = sorted(i for i in lib.running if i not in lib.suspended)
        if not pads.home_from_steam or not ids or self.settings.get("own_overlay", False):
            self._quick_menu_from_desktop()
            return
        answers: list[bool] = []

        def got(has: bool) -> None:
            answers.append(bool(has))
            if len(answers) < len(ids):
                return
            if any(answers):
                log.info("Steam button tapped over a game with Steam's overlay: Steam's (hold it for the quick menu)")
            else:
                self._quick_menu_from_desktop()
        for gid in ids:
            self.bridge.call(self.bridge.core.library_call("has_steam_overlay", gid), got, lambda _e: got(False),
                             owner=self)

    def _bpm_tick(self) -> None:
        from ezgd.ui import steam_bpm
        if self.couch is not None or not self.pages["library"].running:
            self._bpm_timer.stop()
            return
        if steam_bpm.big_picture_in_front() and steam_bpm.close_big_picture():
            QTimer.singleShot(600, self, self._quick_menu_from_desktop)
            for ms in (1800, 3300):                   # after Steam's desktop window came and went again
                QTimer.singleShot(ms, self, lambda: self.couch._regain_screen() if self.couch is not None else None)

    def _quick_menu_from_desktop(self) -> None:
        """The guide button over a game started from the desktop (couch mode closed): the quick menu, with couch
        mode made in the background behind it — shown only if the menu asks for it (couch mode, switch game, …)."""
        if self.couch is not None or not self.pages["library"].running:
            return
        from ezgd.ui.couch import make_couch
        try:
            couch = make_couch(self.actions, list(self.pages["library"].entries.values()),
                                set(self.pages["library"].running), gpu_drawing=not self._couch_gpu_failed)
        except Exception:
            log.exception("the quick menu could not start")
            return
        couch.background = True
        couch.suspended = set(self.pages["library"].suspended)
        couch._target_screen = self.screen() or QApplication.primaryScreen()
        couch.resize(couch._target_screen.geometry().size())   # (never shown: sizes the menu and its prompts)
        couch.on_promote = self._background_couch_shown
        couch.on_desktop = self._back_from_quick_menu
        self.couch = couch
        couch.closed.connect(self._couch_closed)
        if hasattr(couch, "gpu_failed"):
            couch.gpu_failed.connect(self._couch_gpu_failure)
        self._quit_before_bg = QApplication.instance().quitOnLastWindowClosed()
        QApplication.instance().setQuitOnLastWindowClosed(False)    # (the menu closing must not end the app)
        self._watch_quick_button()                        # couch mode's own controllers take over
        couch.guide_pressed()                            # (Steam may answer the same press with its window)
        couch._home_held()

    def _background_couch_shown(self) -> None:
        """The quick menu opened couch mode for real: it replaces this window, as when opened from here."""
        couch = self.couch
        if couch is None:
            return
        self._main_toasts = self.toasts
        self.actions.toasts = couch.toasts
        self.hide()

    def _back_from_quick_menu(self, game_gone: bool) -> None:
        """The quick menu of a game started here is done with couch mode's screens (game settings), or the game ended:
        this window again — couch mode never takes over from a desktop start unless asked for. Raised only when the
        game is gone (not over a game the player goes back to)."""
        self.actions.toasts = getattr(self, "_main_toasts", self.toasts)
        if not self.isVisible():
            self.showNormal()
        if game_gone and not self._quitting:
            self.raise_()
            self.activateWindow()

    def _couch_gpu_failure(self, why: str) -> None:
        """Couch mode's OpenGL drawing did not start: open it again drawn by the CPU (for the rest of this run)."""
        couch = self.couch
        self._couch_gpu_failed = True
        if couch is None or couch.launched or couch.pending_kill:
            return                                # games it started (the ✕ closes them): the CPU next time it opens
        couch.closed.disconnect(self._couch_closed)
        couch.force_close()
        couch.deleteLater()
        self.couch = None
        self.actions.toasts = getattr(self, "_main_toasts", self.toasts)
        self.open_couch()
        if self.couch is not None:
            self.couch.toasts.show("Drawn by the CPU this time", f"GPU acceleration did not start ({why}).", "warning")

    def _couch_closed(self) -> None:
        couch = self.couch
        if couch is not None and couch.background:      # only the quick menu's hidden couch mode: nothing to undo
            self.couch = None
            from ezgd.ui.window_control import release_all
            release_all()
            QApplication.instance().setQuitOnLastWindowClosed(getattr(self, "_quit_before_bg", True))
            self._watch_quick_button()
            return
        if couch is not None and couch.pending_kill:
            # left couch mode (quitting its games) while one was still starting: close it when it appears
            import time as _t
            self._kill_on_start = {i: _t.monotonic() + 90 for i in couch.pending_kill}
            log.info("couch mode closed while %s were starting: they are closed when they appear", couch.pending_kill)
        self.couch = None
        self.actions.toasts = getattr(self, "_main_toasts", self.toasts)
        picked = couch.selected_entry_id() if couch is not None else None
        if picked is not None:                           # the desktop's Library is on the game couch mode was on
            QTimer.singleShot(300, self, lambda: self.pages["library"].select(picked))
        from ezgd.ui.window_control import release_all
        release_all()                                    # "keep above" was for couch mode only
        QTimer.singleShot(600, self, release_all)        # (and once more, after anything still on its way)
        if not self._quitting:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self._reload_library()
            for ms in (300, 1200):                       # (the window manager answers a moment after showing)
                QTimer.singleShot(ms, self, lambda last=ms == 1200: self._check_after_couch(last))
        QApplication.instance().setQuitOnLastWindowClosed(True)
        self._watch_quick_button()
        from ezgd.ui import gcguard                        # couch mode's pictures (hundreds of MB) go back now
        QTimer.singleShot(1500, self, gcguard.release)

    def _check_after_couch(self, last: bool) -> None:
        """Back from couch mode: the desktop window as it was before — not full screen, not covering the whole
        screen (some window managers hand it the size couch mode had), maximized if it was, else its old size. Any
        other size is wrong too: a friend's KWin kept couch mode's "no border" on it — 28 px taller, no title bar
        (1711x989 instead of 1711x961) — so KWin is asked for the title bar and the old size back."""
        saved = getattr(self, "_before_couch", None)
        if saved is None or not self.isVisible() or self.couch is not None:
            return
        size, was_max, was_fs = saved
        screen = self.screen()
        full = screen.geometry() if screen is not None else None
        covers = full is not None and self.width() >= full.width() * 0.97 and self.height() >= full.height() * 0.97
        resized = not was_max and not self.isMaximized() and (abs(self.width() - size.width()) > 2 or
                                                              abs(self.height() - size.height()) > 2)
        wrong = not was_fs and (self.isFullScreen() or (covers and not was_max) or resized or
                                (was_max and not self.isMaximized()))
        log.info("desktop window back from couch mode: %dx%d%s (before: %dx%d%s)%s", self.width(), self.height(),
                 " full screen" if self.isFullScreen() else " maximized" if self.isMaximized() else "",
                 size.width(), size.height(), " maximized" if was_max else " full screen" if was_fs else "",
                 " — putting it back" if wrong else "")
        if wrong:
            self.showNormal()
            if was_max:
                self.showMaximized()
            else:
                self.resize(size)
            from ezgd.ui.window_control import restore_desktop_window
            restore_desktop_window(self.windowTitle(), size.width(), size.height(), was_max)
        if last or not wrong:
            self._before_couch = None if last else saved

    def _launch_requested(self, entry_id: int) -> None:
        """The user starts a game: it is not the one couch mode was left to close (that one was still starting)."""
        pending = getattr(self, "_kill_on_start", None)
        if pending:
            pending.pop(entry_id, None)

    def _close_if_pending(self, payload: dict) -> None:
        """A game started from couch mode appeared after couch mode was left with "Quit game": close it now."""
        import time as _t
        pending = getattr(self, "_kill_on_start", None)
        if not pending or not payload.get("running"):
            return
        gid = payload.get("id")
        deadline = pending.pop(gid, None)
        if deadline is None or _t.monotonic() > deadline:
            return
        title = getattr(self.pages["library"].entries.get(gid), "title", "The game")
        self.bridge.call(self.bridge.core.library_call("stop_completely", gid),
                         lambda r: self.toasts.show(f"{title} closed", "It started after you left couch mode with "
                                                    "“Quit game”.") if r.get("stopped") else
                         self.toasts.show(f"{title} could not be closed", "Close it from the Library (Stop).", "error"),
                         lambda e: self.toasts.show(f"{title} could not be closed", str(e), "error"))

    def open_library_entry(self, entry_id: int) -> None:
        self.navigate("library")
        self.pages["library"].select(entry_id)

    def _focus_search(self) -> None:
        page = self.stack.currentWidget()
        box = getattr(page, "filter", None) or getattr(page, "search", None) or getattr(page, "find", None)
        if box is None:                                # pages without a search box: search everything
            self.navigate("games")
            box = getattr(self.pages["games"], "search", None)
        if box is not None and hasattr(box, "selectAll"):
            box.setFocus(Qt.ShortcutFocusReason)
            box.selectAll()

    def closeEvent(self, e) -> None:
        try:
            if self.isVisible():
                self._ui_state.setValue("geometry", self.saveGeometry())
            self._save_view()
            self._ui_state.sync()
        except Exception:
            pass
        if not self._quitting:
            try:
                summary = self.bridge.run_sync(self.bridge.core.summary(), 5)
            except Exception:
                summary = {}
            active = summary.get("active", 0)
            extracting = summary.get("extracting", 0)
            in_browser = _browser.active_count()
            if active or in_browser or extracting:
                msg = []
                if extracting:
                    msg.append(f"{extracting} game(s) are being installed right now. If you quit, the install "
                               "continues by itself the next time you open the app.")
                if active:
                    msg.append(f"{active} download(s) are running. They will stop now and resume the next time you "
                               "open the app.")
                if in_browser:
                    msg.append(f"{in_browser} in-app browser download(s) are running. These cannot resume — they "
                               "will be cancelled and have to be started again.")
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Question)
                box.setWindowTitle("Quit")
                box.setText(" ".join(msg))
                tray = None
                if self.tray and (active or extracting):
                    tray = box.addButton("Keep downloading in the tray", QMessageBox.AcceptRole)
                    box.setDefaultButton(tray)
                quit_btn = box.addButton("Quit", QMessageBox.DestructiveRole)
                box.addButton(QMessageBox.Cancel)
                box.exec()
                if tray is not None and box.clickedButton() is tray:
                    e.ignore()
                    self.hide()
                    if not self._tray_hint_shown:
                        self._tray_hint_shown = True
                        self.tray.showMessage(APP_DISPLAY_NAME, "Still downloading. Click the tray icon to open the "
                                              "app again; right-click it to quit.", QSystemTrayIcon.Information,
                                              8000)
                    return
                if box.clickedButton() is not quit_btn:
                    e.ignore()
                    return
        self._quitting = True
        if self.couch is not None:
            try:
                self.couch.force_close()
            except RuntimeError:
                pass
            self.couch = None
        self._status_timer.stop()
        self._library_timer.stop()
        self.covers.close()                        # no cover may arrive after the window is gone
        try:
            self.bridge.event.disconnect(self._on_event)
        except (RuntimeError, TypeError):
            pass
        if self.tray:
            self.tray.hide()
        self.launcher.clear()
        e.accept()
