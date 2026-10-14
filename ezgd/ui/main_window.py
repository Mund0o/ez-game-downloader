"""Main window: sidebar navigation, pages, event routing, notifications, shutdown."""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QMainWindow, QMessageBox, QPushButton,
                               QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

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

log = logging.getLogger(__name__)

NAV = [("home", "Home", "home"), ("games", "All Games", "grid"), ("picks", "OnlineFix Picks", "star"),
       ("wishlist", "Wishlist", "heart"), ("downloads", "Downloads", "download"), ("library", "Library", "library"),
       ("settings", "Settings", "settings")]
THEME_KEYS = {"theme_accent", "theme_contrast"}


class MainWindow(QMainWindow):
    def __init__(self, bridge, paths, settings: dict):
        super().__init__()
        self.bridge = bridge
        self.paths = paths
        self.settings = settings
        self._quitting = False
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setWindowIcon(app_icon())
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
        self.couch = None

        self.toasts = ToastHost(self)
        ToastHost.history_changed = lambda: QTimer.singleShot(0, self._notifications_changed)
        self._syscheck_timer = QTimer(self)               # Home's "setup is incomplete" banner stays current
        self._syscheck_timer.setInterval(120_000)
        self._syscheck_timer.timeout.connect(lambda: self.pages["home"].check_system() if hasattr(self, "pages") else None)
        self._syscheck_timer.start()
        self.toasts.reduce_motion = bool(settings.get("reduce_motion"))
        self.covers = CoverCache(bridge, self)
        self.actions = Actions(bridge, self.covers, self.toasts, settings, self)
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
            self.tray.activated.connect(lambda _r: (self.showNormal(), self.raise_(), self.activateWindow()))
            self.tray.show()

        bridge.event.connect(self._on_event)
        self._library_timer = QTimer(self)
        self._library_timer.setSingleShot(True)
        self._library_timer.setInterval(300)
        self._library_timer.timeout.connect(self._reload_library)
        self._wishlist_timer = QTimer(self)
        self._wishlist_timer.setSingleShot(True)
        self._wishlist_timer.setInterval(300)
        self._wishlist_timer.timeout.connect(lambda: self.pages["wishlist"].load())
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._poll_summary)
        self._status_timer.start()

        self._apply_settings_to_pages()
        self.pages["downloads"].load_all()
        self.pages["library"].load_environment()
        self._reload_library()
        self.pages["home"].load()
        self.pages["wishlist"].load()
        self.navigate("home")

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
        super().changeEvent(e)

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
        elif name == "game_state":
            pages["library"].game_state(payload)
            self._close_if_pending(payload)
            if self.couch is not None:
                self.couch.game_state(payload)
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

    def _settings_loaded(self, s: dict, changed_key: str) -> None:
        self.settings.clear()
        self.settings.update(s)
        if changed_key in THEME_KEYS:
            theme.apply_theme(QApplication.instance(), s["theme_accent"], s["theme_contrast"])
            self.navigate(next(k for k, b in self.nav_buttons.items() if b.isChecked()))
            self.toasts.show("Theme updated", "Some colours update fully after a restart.")
        self._apply_settings_to_pages()
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
            text = f"↓ {format_speed(s['speed'])}  ·  {s['active']} active, {s['queued']} queued"
            if s.get("torrent_error"):
                text += f"\n⚠ {s['torrent_error']}"
            self.side_status.setText(text)
            self.pages["downloads"].set_summary(s)
            n = s["active"] + s["queued"]
            self.nav_buttons["downloads"].setText(f"  Downloads ({n})" if n else "  Downloads")
        self.bridge.call(self.bridge.core.summary(), show, owner=self)

    # --------------------------------------------------------------- window
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.toasts.relayout()

    def force_quit(self) -> None:
        self._quitting = True
        self.close()

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
            self.couch.open(self.couch.screen())
            return
        from ezgd.ui.couch import CouchWindow
        screen = self.screen()
        try:
            couch = CouchWindow(self.actions, list(self.pages["library"].entries.values()),
                                set(self.pages["library"].running))
        except Exception:
            log.exception("couch mode could not start")
            self.toasts.show("Couch mode could not start", "The error is in the log.", "error")
            return
        self.couch = couch
        couch.closed.connect(self._couch_closed)
        self._main_toasts = self.toasts
        self.actions.toasts = couch.toasts                 # messages from game actions show in couch mode
        QApplication.instance().setQuitOnLastWindowClosed(False)
        try:
            couch.open(screen)
            self.hide()
        except Exception:
            log.exception("couch mode could not be shown")
            self._couch_closed()

    def _couch_closed(self) -> None:
        couch = self.couch
        if couch is not None and couch.pending_kill:
            # left couch mode (quitting its games) while one was still starting: close it when it appears
            import time as _t
            self._kill_on_start = {i: _t.monotonic() + 90 for i in couch.pending_kill}
            log.info("couch mode closed while %s were starting: they are closed when they appear", couch.pending_kill)
        self.couch = None
        self.actions.toasts = getattr(self, "_main_toasts", self.toasts)
        if not self._quitting:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self._reload_library()
        QApplication.instance().setQuitOnLastWindowClosed(True)

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
        box = getattr(page, "filter", None) or getattr(page, "search", None)
        if box is None:                                # pages without a search box: search everything
            self.navigate("games")
            box = getattr(self.pages["games"], "search", None)
        if box is not None and hasattr(box, "selectAll"):
            box.setFocus(Qt.ShortcutFocusReason)
            box.selectAll()

    def closeEvent(self, e) -> None:
        try:
            self._ui_state.setValue("geometry", self.saveGeometry())
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
                r = QMessageBox.question(self, "Quit", " ".join(msg) + " Quit?")
                if r != QMessageBox.Yes:
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
        try:
            self.bridge.event.disconnect(self._on_event)
        except (RuntimeError, TypeError):
            pass
        if self.tray:
            self.tray.hide()
        e.accept()
