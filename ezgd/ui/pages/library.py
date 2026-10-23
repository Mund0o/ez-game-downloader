"""Library: every downloaded/imported game, played inside the app — Steam-style."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QProgressBar, QScrollArea, QSplitter, QToolButton, QVBoxLayout, QWidget)

from ezgd.library.models import LibraryStatus
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_folder
from ezgd.ui.dialogs.properties import open_properties
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import (Banner, EmptyState, FlowLayout, PasswordBanner, PathLabel, button, card, chip,
                                    clear_layout, label, relative_time, source_chip)

STATUS_TEXT = {"downloaded": "Downloaded", "extracting": "Extracting", "extracted": "Needs setup",
               "installed": "Ready to play", "broken": "Files missing"}
FILTERS = [("all", "All games"), ("favorites", "★ Favourites"), ("ready", "Ready to play"), ("setup", "Needs setup"),
           ("running", "Running"), ("hidden", "Hidden games")]
SORTS = [("recent", "Last played"), ("title", "A–Z"), ("added", "Newest"), ("playtime", "Most played")]


def sort_key(e, how: str):
    """Favourites first, then the chosen order (shared with couch mode's Library)."""
    fav = 0 if e.favorite else 1
    if how == "title":
        return (fav, e.title.casefold())
    if how == "added":
        return (fav, -(e.added_at or 0), e.title.casefold())
    if how == "playtime":
        return (fav, -(e.playtime_seconds or 0), e.title.casefold())
    return (fav, -(e.last_played_at or 0), e.title.casefold())


def _friendly_notes(notes: str | None) -> str:
    """Library notes as the user should read them: OnlineFix torrents' Fix Repair was never needed (the game archive
    has the fix), so "Not extracted automatically: …Fix_Repair…" is said that way."""
    import re
    if not notes:
        return ""
    def fix(m):
        names = [n.strip() for n in m.group(1).split(",")]
        if names and all(re.search(r"fix[ _-]?repair", n, re.I) for n in names):
            return "The Fix Repair archive was not needed — the game archive already contains the fix."
        return m.group(0)
    return re.sub(r"Not extracted automatically: ([^.]*(?:\.(?:rar|zip|7z))?)\.?", fix, notes).strip()


def playtime_text(seconds: int) -> str:
    if seconds < 60:
        return "No playtime yet" if seconds == 0 else "less than a minute"
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


class LibraryPage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.entries: dict[int, object] = {}
        self.env: dict = {"protons": [], "proton_details": [], "steam_root": None, "linux": False, "wine": False,
                          "steam_users": []}
        self.current_id: int | None = None
        self.extraction_state: dict[int, dict] = {}
        self.running: set[int] = set()
        self.suspended: set[int] = set()         # running games frozen from couch mode's quick menu (or here)
        self._password: str | None = None
        actions.covers.ready.connect(self._cover_ready)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 16)
        lay.setSpacing(12)
        head = QHBoxLayout()
        col = QVBoxLayout()
        col.addWidget(label("Library", "title"))
        self.summary = label("", "muted")
        col.addWidget(self.summary)
        head.addLayout(col, 1)
        head.addWidget(button("Check for updates", "update", "ghost",
                              tooltip="Look at every installed game's page for a newer version now",
                              on_click=self._check_all_updates))
        head.addWidget(button("Storage…", "disk", "ghost",
                              tooltip="How much space each game, its prefix and its saves use — and what can go",
                              on_click=self._storage))
        head.addWidget(button("Clean up leftovers…", "trash", "ghost",
                              tooltip="Find game folders and archives that no game in your Library uses any more",
                              on_click=self._leftovers))
        head.addWidget(button("Import game…", "plus", on_click=self.import_game))
        lay.addLayout(head)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search your games…")
        self.filter.textChanged.connect(self._apply_filter)
        ll.addWidget(self.filter)
        self.filter_kind = QComboBox()
        for key, text in FILTERS:
            self.filter_kind.addItem(text, key)
        self.filter_kind.currentIndexChanged.connect(self._apply_filter)
        self.sort = QComboBox()
        for key, text in SORTS:
            self.sort.addItem(text, key)
        self.sort.setToolTip("Order of the list (favourites stay on top)")
        self.sort.currentIndexChanged.connect(lambda _i: self.entries and self._loaded(list(self.entries.values())))
        kinds = QHBoxLayout()
        kinds.setSpacing(6)
        kinds.addWidget(self.filter_kind, 1)
        kinds.addWidget(self.sort, 1)
        ll.addLayout(kinds)
        self.list = QListWidget()
        self.list.setIconSize(QSize(64, 36))
        self.list.setSpacing(2)
        self.list.currentItemChanged.connect(self._selected)
        self._pending_id = None
        self._select_timer = QTimer(self)
        self._select_timer.setSingleShot(True)
        self._select_timer.setInterval(120)
        self._select_timer.timeout.connect(self._render_pending)
        self.list.itemDoubleClicked.connect(self._double_clicked)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)                 # right-click: organise
        self.list.customContextMenuRequested.connect(self._list_menu)
        enter = QShortcut(QKeySequence(Qt.Key_Return), self.list, context=Qt.WidgetShortcut)   # Enter = Play too
        enter.activated.connect(lambda: self.list.currentItem() and self._double_clicked(self.list.currentItem()))
        self.list.setToolTip("Double-click a ready game to play it")
        ll.addWidget(self.list, 1)
        split.addWidget(left)
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.detail = QWidget()
        self.dl = QVBoxLayout(self.detail)
        self.dl.setContentsMargins(12, 0, 4, 12)
        self.dl.setSpacing(14)
        self.detail_scroll.setWidget(self.detail)
        split.addWidget(self.detail_scroll)
        split.setSizes([300, 820])
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        left.setMinimumWidth(220)
        self.detail_scroll.viewport().installEventFilter(self)
        lay.addWidget(split, 1)
        self._show_empty()

    def _double_clicked(self, it: QListWidgetItem) -> None:
        """Double-click = Play, like Steam (only for games that are ready and not running)."""
        e = self.entries.get(it.data(Qt.UserRole))
        if e is None or e.id in self.running or e.status.value != "installed" or not e.executable:
            return
        if (e.setup or {}).get("state") in ("pending", "installing"):
            return
        self.actions.launch(self, e)

    # ----------------------------------------------------------------- data
    def load(self) -> None:
        self.bridge.call(self.bridge.core.library_entries(), self._loaded, owner=self)

    def load_environment(self) -> None:
        self.bridge.call(self.bridge.core.library_environment(), self._env_loaded, owner=self)
        self.bridge.call(self.bridge.core.library_call("running_ids"), self._running_loaded, owner=self)
        self.bridge.call(self.bridge.core.library_call("suspended_ids"), self._suspended_loaded, owner=self)

    def _suspended_loaded(self, ids: list[int]) -> None:
        self.suspended = set(ids)
        self._refresh_items()
        if self.current_id in self.suspended:
            self._render(self.current_id)

    def _env_loaded(self, env: dict) -> None:
        self.env = env
        if self.current_id:
            self._render(self.current_id)

    def _running_loaded(self, ids: list[int]) -> None:
        self.running = set(ids)
        self._refresh_items()

    def _loaded(self, entries: list) -> None:
        self.entries = {e.id: e for e in entries}
        self.actions.installed_keys = {e.dedup_key for e in entries}
        keep = self.current_id
        self.list.blockSignals(True)
        self.list.clear()
        self._fill_kinds(entries)
        how = self.sort.currentData() or "recent"
        for e in sorted(entries, key=lambda x: sort_key(x, how)):
            it = QListWidgetItem()
            it.setData(Qt.UserRole, e.id)
            it.setSizeHint(QSize(100, 48))
            self.list.addItem(it)
            if e.id == keep:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        self._refresh_items()
        ready = sum(1 for e in entries if e.status is LibraryStatus.INSTALLED)
        total_play = sum(e.playtime_seconds for e in entries)
        self.summary.setText(f"{len(entries)} games · {ready} ready to play"
                             + (f" · {playtime_text(total_play)} played" if total_play >= 60 else ""))
        self._apply_filter()
        if keep in self.entries:
            self._render(keep)
        elif entries:
            self.list.setCurrentRow(0)
        else:
            self._show_empty()

    def _refresh_items(self) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            e = self.entries.get(it.data(Qt.UserRole))
            if e is None:
                continue
            status = self._status_text(e)
            src = theme.source_name(e.source) if e.source else "Imported"
            it.setText(f"{'★ ' if e.favorite else ''}{e.title}\n{status} · {src}")
            it.setIcon(self._thumb(e))

    def _status_text(self, e) -> str:
        state = (e.setup or {}).get("state")
        if e.id in self.suspended:
            return "Suspended"
        if e.id in self.running:
            return "Installing" if state == "installing" else "Running"
        if state == "pending":
            return "Ready to install"
        if state == "waiting_steam":
            return "Waiting for Steam"
        return STATUS_TEXT.get(str(e.status), str(e.status))

    def _thumb(self, e) -> QIcon:
        pm = self.actions.covers.get(e.cover_url) if e.cover_url else None
        if pm is not None and not pm.isNull():
            return QIcon(pm.scaled(QSize(128, 72), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                         .copy(0, 0, 128, 72))
        return icon("library", theme.status_color("installed" if e.id in self.running else str(e.status)), 36)

    def _cover_ready(self, url: str) -> None:
        if any(e.cover_url == url for e in self.entries.values()):
            self._refresh_items()
            if self.current_id and self.entries.get(self.current_id) and \
                    self.entries[self.current_id].cover_url == url:
                self._set_hero_cover(self.entries[self.current_id])

    def _apply_filter(self) -> None:
        q = self.filter.text().strip().lower()
        kind = self.filter_kind.currentData()
        for i in range(self.list.count()):
            it = self.list.item(i)
            e = self.entries.get(it.data(Qt.UserRole))
            if e is None:
                continue
            ok = not q or q in e.title.lower()
            if kind == "hidden":
                ok = ok and e.hidden
            elif e.hidden:
                ok = False                                 # hidden games: only under "Hidden games"
            if kind == "favorites":
                ok = ok and e.favorite
            elif isinstance(kind, str) and kind.startswith("c:"):
                ok = ok and kind[2:] in e.collections
            elif kind == "ready":
                ok = ok and e.status is LibraryStatus.INSTALLED
            elif kind == "setup":
                ok = ok and e.status is not LibraryStatus.INSTALLED
            elif kind == "running":
                ok = ok and e.id in self.running
            it.setHidden(not ok)

    def _fill_kinds(self, entries) -> None:
        """The filter list: the fixed ones, then one per collection."""
        names = sorted({c for e in entries for c in e.collections}, key=str.casefold)
        want = [(k, t) for k, t in FILTERS] + [(f"c:{n}", f"▸ {n}") for n in names]
        have = [(self.filter_kind.itemData(i), self.filter_kind.itemText(i)) for i in range(self.filter_kind.count())]
        if want == have:
            return
        cur = self.filter_kind.currentData()
        self.filter_kind.blockSignals(True)
        self.filter_kind.clear()
        for k, t in want:
            self.filter_kind.addItem(t, k)
        self.filter_kind.setCurrentIndex(max(0, self.filter_kind.findData(cur)))
        self.filter_kind.blockSignals(False)

    # ---------------------------------------------------------------- organising
    def _organize(self, e, **kw) -> None:
        def done(_r) -> None:
            self.load()
        self.bridge.call(self.bridge.core.library_call("organize", e.id, **kw), done,
                         lambda err: self.actions.fail("Library", err), owner=self)

    def _new_collection(self, e) -> None:
        name, ok = QInputDialog.getText(self, "New collection", f"Put “{e.title}” into a new collection:")
        if ok and name.strip():
            self._organize(e, collections=e.collections + [name.strip()])

    def _organize_menu(self, m: QMenu, e) -> None:
        m.addAction("☆ Remove from favourites" if e.favorite else "★ Add to favourites",
                    lambda: self._organize(e, favorite=not e.favorite))
        col = m.addMenu("Collections")
        names = sorted({c for x in self.entries.values() for c in x.collections}, key=str.casefold)
        for n in names:
            a = col.addAction(n)
            a.setCheckable(True)
            a.setChecked(n in e.collections)
            a.toggled.connect(lambda on, n=n: self._organize(
                e, collections=[c for c in e.collections if c != n] + ([n] if on else [])))
        if names:
            col.addSeparator()
        col.addAction("New collection…", lambda: self._new_collection(e))
        m.addAction("Show in the Library again" if e.hidden else "Hide from the Library",
                    lambda: self._organize(e, hidden=not e.hidden))

    def _list_menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        e = self.entries.get(it.data(Qt.UserRole)) if it is not None else None
        if e is None:
            return
        m = QMenu(self)
        self._organize_menu(m, e)
        m.exec(self.list.viewport().mapToGlobal(pos))

    def _selected(self, item: QListWidgetItem | None, _prev) -> None:
        """A click shows the game at once; holding an arrow key through the list shows the one it stops on (each
        page is ~40 widgets and eight look-ups — building one per passing game made the list lag behind)."""
        if item is None:
            return
        import time as _t
        now = _t.monotonic()
        busy = now - getattr(self, "_last_select", 0.0) < 0.2
        self._last_select = now
        self._pending_id = item.data(Qt.UserRole)
        if not busy:
            self._select_timer.stop()
            self._render(self._pending_id)
        else:
            self._select_timer.start()

    def _render_pending(self) -> None:
        if self._pending_id is not None:
            self._render(self._pending_id)

    def _show_empty(self) -> None:
        clear_layout(self.dl)
        e = EmptyState("library", "Your library is empty",
                       "Every game you download appears here automatically, ready to set up and play. You can also "
                       "import a game folder or an archive you downloaded in your browser.")
        e.buttons.addWidget(button("Import game…", "plus", on_click=self.import_game))
        self.dl.addWidget(e)

    # --------------------------------------------------------------- events
    def extraction(self, state: dict) -> None:
        lid = state.get("library_id")
        if lid is None:
            return
        self.extraction_state[lid] = state
        if state["status"] in ("completed", "failed"):
            self.load()
        elif lid == self.current_id and hasattr(self, "_xbar"):
            try:
                self._xbar.setValue(int(state["progress"] * 1000))
                self._xlabel.setText(f"Extracting {state['archive']} — {state['progress'] * 100:.0f}% "
                                     f"{state.get('item', '')[:60]}")
            except RuntimeError:
                pass

    def game_state(self, payload: dict) -> None:
        gid = payload["id"]
        if "suspended" in payload:                     # frozen / continued (couch mode's quick menu, or here)
            (self.suspended.add if payload["suspended"] else self.suspended.discard)(gid)
        if payload["running"]:
            self.running.add(gid)
        else:
            self.running.discard(gid)
            self.suspended.discard(gid)
            played = payload.get("played", 0)
            e = self.entries.get(gid)
            if e is not None and played >= 60:
                self.actions.toasts.show("Session ended", f"{e.title}: played {playtime_text(played)}.")
        self._refresh_items()
        if gid == self.current_id:
            self._render(gid)

    # --------------------------------------------------------------- render
    def _render(self, entry_id: int) -> None:
        e = self.entries.get(entry_id)
        if e is None:
            return
        self.current_id = entry_id
        self._render_gen = getattr(self, "_render_gen", 0) + 1
        gen = self._render_gen
        clear_layout(self.dl)
        status = str(e.status)
        running = e.id in self.running

        hero = QFrame()
        hero.setObjectName("hero")
        hl = QHBoxLayout(hero)
        hl.setContentsMargins(18, 18, 18, 18)
        hl.setSpacing(20)
        self._hero_cover = QLabel()
        self._hero_cover.setFixedSize(*(self._cover_size() if self._cover_size()[0] else (160, 90)))
        self._hero_cover.setAlignment(Qt.AlignCenter)
        self._hero_cover.setStyleSheet(f"background: {theme.CURRENT.bg3}; border-radius: 12px;")
        self._set_hero_cover(e)
        hl.addWidget(self._hero_cover, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(10)
        col.addWidget(label(e.title, "title", wrap=True))
        chips = FlowLayout(spacing=6)
        chips.addWidget(source_chip(e.source) if e.source else chip("Imported manually", theme.CURRENT.muted))
        if e.id in self.suspended:
            chips.addWidget(chip("❚❚ Suspended — continues where it was", theme.CURRENT.warning))
        elif running:
            chips.addWidget(chip("● " + self._status_text(e), theme.CURRENT.success))
        else:
            chips.addWidget(chip(self._status_text(e), theme.status_color(status)))
        col.addLayout(chips)
        play_row = FlowLayout(spacing=8)          # wraps under Play when the window is narrow
        play_row.addWidget(self._primary_button(e, running))
        if e.id in self.suspended:
            stop = button("  Stop", "close", on_click=lambda: self._stop(e))
            stop.setProperty("variant", "stop")
            stop.setMinimumHeight(44)
            play_row.addWidget(stop)
        self._fixes_btn = button("  Fixes", "wrench", tooltip="Fixes for this game: tick the ones you want to try",
                                 on_click=lambda: self._open_fixes(e))
        self._fixes_btn.setMinimumHeight(44)
        self._fixes_btn.setEnabled(bool(e.executable))
        play_row.addWidget(self._fixes_btn)
        installing = (e.setup or {}).get("state") in ("pending", "installing")
        self._fixes_btn.setVisible(not installing)          # nothing to fix before the game is installed
        diag = button("  Diagnose", "bug", tooltip="Debugging assistant: finds out why the game does not run and "
                                                    "recommends the fix that matches the evidence",
                      on_click=lambda: self._diagnose(e))
        diag.setMinimumHeight(44)
        diag.setEnabled(bool(e.executable))
        diag.setVisible(not installing)
        play_row.addWidget(diag)
        if e.executable:
            self.bridge.call(self.bridge.core.library_call("fixes_for", e.id),
                             lambda items: gen == self._render_gen and self._fix_count(items), owner=self)
        props = QToolButton()
        props.setIcon(icon("settings", theme.CURRENT.text))
        props.setIconSize(QSize(22, 22))
        props.setToolTip("Properties (launch options, Proton, fixes)")
        props.clicked.connect(lambda: self._properties(e))
        chips.addWidget(props)
        folder = QToolButton()
        folder.setIcon(icon("folder", theme.CURRENT.text))
        folder.setIconSize(QSize(22, 22))
        folder.setToolTip("Open the game folder")
        folder.clicked.connect(lambda: open_folder(e.install_path or e.download_path or ""))
        folder.setEnabled(bool(e.install_path or e.download_path))
        chips.addWidget(folder)
        more = QToolButton()
        more.setIcon(icon("more", theme.CURRENT.text))
        more.setIconSize(QSize(22, 22))
        more.setToolTip("More")
        more.clicked.connect(lambda: self._more_menu(e, more))
        chips.addWidget(more)
        col.addLayout(play_row)
        self._runs_with = label("", "small", wrap=True)
        col.addWidget(self._runs_with)
        self._failed_holder = QVBoxLayout()
        col.addLayout(self._failed_holder)
        if e.executable and not running and e.status is LibraryStatus.INSTALLED:
            self.bridge.call(self.bridge.core.library_call("last_run_status", e.id),
                             lambda st: gen == self._render_gen and self._last_run_failed(e, st), owner=self)
            self.bridge.call(self.bridge.core.preflight(e.id),
                             lambda items: gen == self._render_gen and self._show_preflight(e, items), owner=self)
        if e.executable:
            self.bridge.call(self.bridge.core.library_call("preview", e.id), self._show_runs_with, owner=self)
        col.addStretch(1)
        hl.addLayout(col, 1)
        self.dl.addWidget(hero)

        stats = FlowLayout(spacing=10)            # wraps to two rows in a narrow window
        for title, value in (("Play time", playtime_text(e.playtime_seconds)),
                             ("Last played", relative_time(e.last_played_at) if e.last_played_at else "Never"),
                             ("Launches", str(e.play_count)),
                             ("Steam App ID", str(e.steam_app_id) if e.steam_app_id else "Unknown")):
            c, cl = card(margins=(14, 10, 14, 10), spacing=2)
            cl.addWidget(label(title, "small"))
            v = label(value)
            v.setStyleSheet("font-size: 12pt; font-weight: 700;")
            cl.addWidget(v)
            c.setMinimumWidth(150)
            stats.addWidget(c)
        self.dl.addLayout(stats)

        # async results are dropped if the page was re-rendered in the meantime (stale generation)
        self._resume_holder = QVBoxLayout()
        self.dl.addLayout(self._resume_holder)
        if e.status is not LibraryStatus.EXTRACTING:
            self.bridge.call(self.bridge.core.interrupted_install(e.id),
                             lambda info: gen == self._render_gen and self._show_interrupted(e, info), owner=self)
        if e.game_id and e.status in (LibraryStatus.INSTALLED, LibraryStatus.EXTRACTED):
            self.dl.addWidget(self._update_card(e))
        self._advice_holder = QVBoxLayout()
        self.dl.addLayout(self._advice_holder)
        if e.executable:
            self.bridge.call(self.bridge.core.library_call("suggestions", e.id),
                             lambda s: gen == self._render_gen and self._show_suggestions(e, s), owner=self)
        self._password_holder = QVBoxLayout()
        self.dl.addLayout(self._password_holder)
        self._password = None
        self.bridge.call(self.bridge.core.entry_password(e.id),
                         lambda pw, s=e.source: gen == self._render_gen and self._show_password(s, pw), owner=self)
        self._dupe_holder = QVBoxLayout()
        self.dl.addLayout(self._dupe_holder)
        self.bridge.call(self.bridge.core.library_call("duplicates", e.id),
                         lambda d: gen == self._render_gen and self._dupes(d), owner=self)
        if e.status is LibraryStatus.BROKEN:
            self.dl.addWidget(Banner("error", "The files for this game were not found on disk. They may have been "
                                              "moved or deleted; change the install folder or remove the entry."))
        notes = _friendly_notes(e.notes)
        if notes:
            self.dl.addWidget(Banner("info", notes))
        self._check_holder = QVBoxLayout()
        self.dl.addLayout(self._check_holder)
        self.bridge.call(self.bridge.core.new_game_check(e.id),
                         lambda st: gen == self._render_gen and self._show_check(e, st), lambda _err: None, owner=self)

        xs = self.extraction_state.get(e.id)
        if e.status is LibraryStatus.EXTRACTING and xs:
            xc, xl = card()
            xl.addWidget(label("Extracting", "h2"))
            self._xlabel = label(f"Extracting {xs['archive']}…", "muted")
            self._xbar = QProgressBar()
            self._xbar.setRange(0, 1000)
            self._xbar.setValue(int(xs.get("progress", 0) * 1000))
            xl.addWidget(self._xlabel)
            xl.addWidget(self._xbar)
            if xs.get("id"):
                xl.addWidget(button("Cancel extraction", "close", "danger",
                                    on_click=lambda: self.bridge.call(self.bridge.core.cancel_extraction(xs["id"]))))
            self.dl.addWidget(xc)
        elif (e.setup or {}).get("state") in ("pending", "installing"):
            self.dl.addWidget(self._installer_card(e, gen))
        elif e.status is not LibraryStatus.INSTALLED:
            self.dl.addWidget(self._setup_card(e))
        if (e.setup or {}).get("state") == "installed" or (not e.setup and e.status is not LibraryStatus.INSTALLED):
            # repack files left after installing, or an older download that turns out to be an installer
            self.bridge.call(self.bridge.core.library_call("installer_info", e.id),
                             lambda info: gen == self._render_gen and self._repack_left(e, info), owner=self)

        self._saves_holder = QVBoxLayout()
        self.dl.addLayout(self._saves_holder)
        if self.env.get("linux") and e.executable and e.executable.lower().endswith((".exe", ".bat", ".cmd")):
            self.bridge.call(self.bridge.core.library_call("save_backups", e.id),
                             lambda r: gen == self._render_gen and self._show_saves(e, r), owner=self)
        self.dl.addWidget(self._files_card(e))
        self.dl.addStretch(1)

    def select(self, entry_id: int) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == entry_id:
                self._last_select = 0.0                   # chosen by the app: shown at once
                self.list.setCurrentRow(i)
                if self.current_id != entry_id:
                    self._select_timer.stop()
                    self._render(entry_id)
                return

    # ---------------------------------------------------------- interrupted installs
    def _show_interrupted(self, e, info: dict | None) -> None:
        if not info:
            return
        b = Banner("warning", f"Installing {e.title} was interrupted when the app closed ({info['name']}). The "
                              "archive is still there — finish the install now.")
        btn = b.add_action("Finish installing", None, "download")

        def go(_=False) -> None:
            btn.setEnabled(False)
            btn.setText("Installing…")
            self.extraction_state[e.id] = {"id": None, "archive": info["name"], "progress": 0.0, "status": "running"}
            self.bridge.call(self.bridge.core.finish_install(e.id), lambda _ok: self.load(),
                             lambda err: (self.actions.fail("Could not finish the install", err), self.load()),
                             owner=self)
            self.load()
        btn.clicked.connect(go)
        try:
            self._resume_holder.addWidget(b)
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- updates
    def _update_card(self, e) -> QFrame:
        from ezgd.library import updates
        rel = e.release or {}
        c, cl = card(margins=(16, 12, 16, 12), spacing=8)
        row = QHBoxLayout()
        ic = QLabel()
        st = rel.get("status")
        color = theme.CURRENT.warning if st in ("update", "different") else theme.CURRENT.success \
            if st == "current" else theme.CURRENT.muted
        ic.setPixmap(icon("update", color, 22).pixmap(22, 22))
        row.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        if rel.get("updating"):
            title = "Updating — waiting for the new version"
            text = ("Download the new version from the game's page (or import the file you downloaded in your "
                    "browser): it is extracted over the installed game. Your saves are backed up first; your "
                    "settings, fixes and Proton prefix are kept.")
        elif st == "update":
            title = f"Update available: {rel.get('latest')}"
            text = f"You have {rel.get('version')}. " + ("Updating downloads the new release and puts it over the "
                                                         "installed game — saves, settings and fixes are kept.")
        elif st == "different":
            title = f"The page lists version {rel.get('latest')}"
            text = f"You have {rel.get('version')}. The app cannot tell which is newer — compare them on the page."
        elif rel.get("version"):
            title = f"Version {rel['version']}"
            text = updates.describe(rel) + (f" Checked {relative_time(rel['checked_at'])}." if rel.get("checked_at")
                                            else "")
        else:
            title = "Version unknown"
            text = "The app checks the game's page for newer versions every few hours."
        t = label(title)
        t.setStyleSheet("font-weight: 700; font-size: 11pt;")
        col.addWidget(t)
        col.addWidget(label(text, "small", wrap=True))
        row.addLayout(col, 1)
        if rel.get("updating"):
            row.addWidget(button("Open game page", "external", on_click=lambda: self._open_update_page(e)))
            row.addWidget(button("Cancel update", "close", "ghost", on_click=lambda: self.bridge.call(
                self.bridge.core.cancel_game_update(e.id), lambda _r: self.load(), owner=self)))
        elif st in ("update", "different"):
            b = button(f"  Update to {rel.get('latest')}", "download", on_click=lambda: self._start_update(e))
            b.setProperty("variant", "primary")
            row.addWidget(b)
        else:
            row.addWidget(button("Check now", "refresh", "ghost", on_click=lambda: self._check_update(e)))
        cl.addLayout(row)
        if st == "update":
            c.setStyleSheet(f"QFrame#card {{ border-color: {theme.CURRENT.warning}; }}")
        return c

    def _check_update(self, e) -> None:
        self.actions.toasts.show("Checking for an update…", e.title)

        def done(res: list[dict]) -> None:
            r = next((x for x in res if x.get("entry_id") == e.id), None)
            if r is None:
                self.actions.toasts.show("No update information", "This game's page does not say which version it "
                                                                   "has, or the game is not linked to a page.")
            elif r.get("error"):
                self.actions.toasts.show("Could not check", r["error"], "warning")
            else:
                self.actions.toasts.show("Update available" if r["status"] == "update" else "Checked", r["text"],
                                         "success" if r["status"] == "update" else "info")
            self.load()
        self.bridge.call(self.bridge.core.check_game_updates(e.id, True), done,
                         lambda err: self.actions.fail("Update check", err), owner=self)

    def _check_all_updates(self) -> None:
        self.actions.toasts.show("Checking for updates…", "Reading every installed game's page.")

        def done(res: list[dict]) -> None:
            ups = [r for r in res if r.get("status") == "update"]
            if ups:
                self.actions.toasts.show(f"{len(ups)} update(s) available", ", ".join(r["title"] for r in ups[:5]),
                                         "success")
            else:
                self.actions.toasts.show("Everything is up to date", f"{len(res)} game(s) checked.")
            self.load()
        self.bridge.call(self.bridge.core.check_game_updates(None, True), done,
                         lambda err: self.actions.fail("Update check", err), owner=self)

    def _start_update(self, e) -> None:
        def begun(info: dict) -> None:
            self.load()
            self._open_update_page(e, info)
        self.bridge.call(self.bridge.core.begin_game_update(e.id), begun,
                         lambda err: self.actions.fail("Update", err), owner=self)

    def _open_update_page(self, e, info: dict | None = None) -> None:
        from ezgd.catalog.models import GameGroup
        from ezgd.ui.dialogs.game_details import GameDetailsDialog

        def got(game) -> None:
            if game is None:
                self.actions.toasts.show("Game page unknown", "Search for the game in All Games instead.", "warning")
                return
            rel = e.release or {}
            update = info or {"title": e.title, "installed": rel.get("version"), "latest": rel.get("latest"),
                              "dest": e.install_path}
            GameDetailsDialog(self.actions, GameGroup(game.dedup_key, [game]), self, update=update).exec()
            self.load()
        self.bridge.call(self.bridge.core.game(e.game_id), got, owner=self)

    # ------------------------------------------------------------ save backups
    def saves_changed(self, entry_id) -> None:
        if entry_id == self.current_id and self.current_id is not None:
            gen = self._render_gen
            e = self.entries.get(entry_id)
            if e is not None:
                self.bridge.call(self.bridge.core.library_call("save_backups", entry_id),
                                 lambda r: gen == self._render_gen and self._show_saves(e, r), owner=self)

    def _show_saves(self, e, r: dict) -> None:
        from ezgd.core.fsutil import format_bytes
        try:
            clear_layout(self._saves_holder)
        except RuntimeError:
            return
        c, cl = card()
        head = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icon("save", theme.CURRENT.accent, 22).pixmap(22, 22))
        head.addWidget(ic)
        head.addWidget(label("Saves", "h2"), 1)
        b_now = button("Back up now", "save", on_click=lambda: self._backup_now(e))
        b_now.setEnabled(bool(r.get("has_prefix")))
        head.addWidget(b_now)
        head.addWidget(button("", "folder", "ghost", tooltip="Open the backups folder",
                              on_click=lambda: open_folder(r["folder"])))
        cl.addLayout(head)
        backups = r.get("backups") or []
        if r.get("has_prefix"):
            where = ", ".join(r.get("current_folders") or []) or "no save files found yet"
            cl.addWidget(label(f"In the game's prefix now: {r.get('current_files', 0)} file(s), "
                               f"{format_bytes(r.get('current_bytes') or 0)} — {where}", "small", wrap=True))
        else:
            cl.addWidget(label("The game's Proton prefix is created the first time it starts; its saves can be backed "
                               "up after that.", "small", wrap=True))
        auto = self.actions.settings.get("auto_backup_saves", True)
        cl.addWidget(label(("Backed up automatically after every session, and before updates, uninstalls and prefix "
                            "rebuilds. " if auto else "Automatic backups are off (Settings → Library). ")
                           + "Backups are kept per game title, so a reinstall finds them again"
                           + ("." if self.actions.settings.get("keep_more_save_backups", False) else
                              " — the newest 3; a new one deletes the oldest (Settings → Library to keep more)."),
                           "small", wrap=True))
        if not backups:
            cl.addWidget(label("No backups yet.", "muted"))
        for b in backups[:6]:
            row = QHBoxLayout()
            when = relative_time(b["created"])
            t = label(f"{b['reason_text']} · {when}")
            t.setStyleSheet("font-weight: 600;")
            t.setToolTip(b["path"] + ("\n" + "\n".join(b.get("folders") or []) if b.get("folders") else ""))
            row.addWidget(t)
            row.addWidget(label(f"{b['files']} file(s) · {format_bytes(b['bytes'])}", "small"), 1)
            row.addWidget(button("Restore…", "retry", "ghost", on_click=lambda b=b: self._restore_saves(e, b)))
            row.addWidget(button("", "trash", "ghost", tooltip="Delete this backup",
                                 on_click=lambda b=b: self._delete_backup(e, b)))
            cl.addLayout(row)
        if len(backups) > 6:
            cl.addWidget(label(f"{len(backups) - 6} older backup(s) in the folder.", "small"))
        self._saves_holder.addWidget(c)

    def _backup_now(self, e) -> None:
        def done(b) -> None:
            if b:
                self.actions.toasts.show("Saves backed up", f"{e.title}: {b['files']} file(s).", "success")
            else:
                self.actions.toasts.show("Nothing to back up", f"{e.title} has no save files in its prefix yet.")
        self.bridge.call(self.bridge.core.library_call("backup_saves", e.id, "manual"), done,
                         lambda err: self.actions.fail("Save backup", err), owner=self)

    def _restore_saves(self, e, b: dict) -> None:
        if e.id in self.running:
            QMessageBox.information(self, "Restore saves", "Quit the game first — it would overwrite the restored "
                                                           "saves when it exits.")
            return
        r = QMessageBox.question(self, "Restore saves",
                                 f"Put the saves from “{b['reason_text']}” ({relative_time(b['created'])}, "
                                 f"{b['files']} files) back into {e.title}?\n\nYour current saves are backed up "
                                 "first, so you can go back.")
        if r != QMessageBox.Yes:
            return
        self.bridge.call(self.bridge.core.library_call("restore_save_backup", e.id, b["path"]),
                         lambda res: self.actions.toasts.show("Saves restored", f"{res['files']} file(s) put back. "
                                                              "The saves you had before are in the list too.",
                                                              "success"),
                         lambda err: self.actions.fail("Could not restore", err), owner=self)

    def _delete_backup(self, e, b: dict) -> None:
        r = QMessageBox.question(self, "Delete backup", f"Delete the save backup “{b['reason_text']}” from "
                                                        f"{relative_time(b['created'])}?")
        if r == QMessageBox.Yes:
            self.bridge.call(self.bridge.core.library_call("delete_save_backup", e.id, b["path"]), None,
                             lambda err: self.actions.fail("Delete backup", err), owner=self)

    def _storage(self) -> None:
        from ezgd.ui.dialogs.storage import StorageDialog
        StorageDialog(self.actions, self).exec()
        self.load()

    def _primary_button(self, e, running: bool):
        state = (e.setup or {}).get("state")
        if e.id in self.suspended:
            b = button("  Resume", "play", tooltip="It continues exactly where it was frozen",
                       on_click=lambda: self._resume(e))
            b.setProperty("variant", "play")
            b.setIcon(icon("play", "#ffffff"))
        elif running:
            b = button("  Stop installer" if state == "installing" else "  Stop", "close",
                       on_click=lambda: self._stop(e))
            b.setProperty("variant", "stop")
            b.setIcon(icon("close", "#ffffff"))
        elif e.status is LibraryStatus.EXTRACTING:
            b = button("  Extracting…", "refresh")
            b.setProperty("variant", "play")
            b.setEnabled(False)
        elif state == "installing":
            b = button("  Installing…", "refresh", tooltip="The installer is still running in its own window")
            b.setProperty("variant", "play")
            b.setEnabled(False)
        elif state == "waiting_steam":
            b = button("  Install in Steam", "download",
                       tooltip=f"Install “{e.setup.get('base')}” in Steam (free); the app then adds the fix to it "
                               "by itself", on_click=lambda: self._install_steam_base(e))
            b.setProperty("variant", "play")
            b.setIcon(icon("download", "#ffffff"))
        elif state == "pending":
            b = button("  Install game", "download", tooltip="Run this repack's installer (setup.exe) — you confirm "
                                                             "first", on_click=lambda: self._install(e))
            b.setProperty("variant", "play")
            b.setIcon(icon("download", "#ffffff"))
        elif not e.executable:
            b = button("  Set up", "settings", on_click=lambda: self._properties(e))
            b.setProperty("variant", "setup")
            b.setIcon(icon("settings", "#ffffff"))
        else:
            b = button("  Play", "play", on_click=lambda: self.actions.launch(self, self.entries[e.id]))
            b.setProperty("variant", "play")
            b.setIcon(icon("play", "#ffffff"))
            b.setEnabled(e.status is not LibraryStatus.BROKEN)
        b.setIconSize(QSize(20, 20))
        return b

    def _show_preflight(self, e, items: list[dict]) -> None:
        """Problems visible before the game is even started — each with its one-click fix."""
        for f in items:
            b = Banner("warning", f"Before you play: {f['title']}. " + f["explanation"].split(". ")[0] + ".")
            btn = b.add_action(f["fix"]["label"], None, "wrench")

            def run(_=False, f=f, btn=btn) -> None:
                btn.setEnabled(False)
                btn.setText("Installing… (a few minutes)" if f["fix"]["kind"] == "winetricks" else "Working…")

                def done(msg: str) -> None:
                    self.actions.toasts.show("Fixed", msg, "success")
                    if self.current_id == e.id:
                        self._render(e.id)

                def failed(exc) -> None:
                    try:
                        btn.setEnabled(True)
                        btn.setText(f["fix"]["label"])
                    except RuntimeError:
                        pass
                    self.actions.fail("Could not apply the fix", exc)
                self.bridge.call(self.bridge.core.apply_fix(e.id, f["fix"]["kind"], f["fix"]["value"]), done,
                                 failed, owner=self)
            btn.clicked.connect(run)
            try:
                self._failed_holder.addWidget(b)
            except RuntimeError:
                return

    def _last_run_failed(self, e, st: dict) -> None:
        if not st.get("failed"):
            return
        text = f"The last start ended after {st['seconds']}s (exit code {st['exit_code']})"
        text += f" — {st['meaning'].split(':')[0]}." if st.get("meaning") else "."
        b = Banner("warning", text + " Diagnose finds out why and offers the fix.")
        b.add_action("Diagnose", lambda: self._diagnose(e), "bug")
        try:
            self._failed_holder.addWidget(b)
        except RuntimeError:
            pass

    def _install_steam_base(self, e) -> None:
        from ezgd.ui.dialogs.misc import open_url

        def done(r: dict) -> None:
            if r.get("opened") == "store" and r.get("url"):
                open_url(r["url"])
            self.actions.toasts.show("Install it in Steam", f"Press “Install” for {r['name']} in Steam. When Steam "
                                     "has finished, the app adds the fix to it and the game is ready to play here.")
        if e.game_id:
            self.bridge.call(self.bridge.core.install_steam_base(e.game_id), done,
                             lambda exc: self.actions.fail("Steam", exc), owner=self)

    def eventFilter(self, obj, ev) -> bool:                      # noqa: N802 (Qt API)
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.Resize and obj is self.detail_scroll.viewport():
            sp = self.detail_scroll.parentWidget()
            if hasattr(sp, "sizes") and self.width() < 900 and sp.sizes()[0] > 240:
                sp.setSizes([240, max(300, sum(sp.sizes()) - 240)])   # small window: a narrower list
            cover = getattr(self, "_hero_cover", None)
            e = self.entries.get(self.current_id) if self.current_id else None
            try:
                if cover is not None and e is not None and cover.width() != self._cover_size()[0]:
                    self._set_hero_cover(e)
            except RuntimeError:
                pass
        return super().eventFilter(obj, ev)

    def _cover_size(self) -> tuple[int, int]:
        """A smaller cover when the details pane is narrow (it pushed the Play button out of view)."""
        w = self.detail_scroll.viewport().width() if hasattr(self, "detail_scroll") else 900
        return (320, 180) if w >= 820 else (224, 126) if w >= 600 else (160, 90) if w >= 500 else (0, 0)

    def _set_hero_cover(self, e) -> None:
        try:
            cw, ch = self._cover_size()
            self._hero_cover.setVisible(cw > 0)                  # no room: no cover (the list shows it)
            if not cw:
                return
            if self._hero_cover.width() != cw:
                self._hero_cover.setFixedSize(cw, ch)
            pm = self.actions.covers.get(e.cover_url) if e.cover_url else None
            if pm is not None and not pm.isNull():
                self._hero_cover.setPixmap(pm.scaled(QSize(cw, ch), Qt.KeepAspectRatioByExpanding,
                                                     Qt.SmoothTransformation).copy(0, 0, cw, ch))
            else:
                self._hero_cover.setPixmap(icon("library", theme.CURRENT.muted, 64).pixmap(64, 64))
        except RuntimeError:
            pass

    def _show_runs_with(self, p: dict) -> None:
        try:
            self._runs_with.setText(("Runs with: " + p["description"]) if p["ok"] else ("⚠ " + p["error"]))
        except RuntimeError:
            pass

    def _setup_card(self, e) -> QFrame:
        c, cl = card()
        cl.addWidget(label("Finish setting up", "h2"))
        has_files = bool(e.install_path)
        steps = [
            (has_files, "Game files extracted / folder chosen",
             None if has_files else ("Choose folder…", lambda: self._change_install(e))),
            (bool(e.executable), "Executable selected",
             None if e.executable else ("Choose…", lambda: self._properties(e))),
        ]
        if self.env.get("linux") and e.executable and e.executable.lower().endswith((".exe", ".bat")):
            has_proton = bool(self.env.get("protons"))
            steps.append((has_proton, "Proton available to run this Windows game", None))
        steps.append((e.status is LibraryStatus.INSTALLED, "Marked as installed",
                      ("Mark as installed", lambda: self._mark_installed(e))))
        for done, text, action in steps:
            row = QHBoxLayout()
            ic = QLabel()
            ic.setPixmap(icon("check" if done else "info",
                              theme.CURRENT.success if done else theme.CURRENT.muted, 18).pixmap(18, 18))
            row.addWidget(ic)
            row.addWidget(label(text, None if done else "muted"), 1)
            if action and not done:
                row.addWidget(button(action[0], variant="ghost", on_click=action[1]))
            cl.addLayout(row)
        cl.addWidget(label("You can press Play as soon as an executable is selected; marking a game as installed "
                           "just files it under “Ready to play”.", "small", wrap=True))
        return c

    # ------------------------------------------------------------ installers
    def _installer_card(self, e, gen: int) -> QFrame:
        c, cl = card()
        installing = (e.setup or {}).get("state") == "installing"
        cl.addWidget(label("Installing the game…" if installing else "Install the game first", "h2"))
        proton = " with Proton" if self.env.get("linux") else ""
        if installing:
            text = ("The installer is open in its own window. Finish it there — when it closes, the app finds the "
                    "installed game and selects its executable for Play. Unpacking a repack can take a while.")
        else:
            text = ("This download is an installer (a repack), not a ready-to-play game folder. Press “Install "
                    f"game”: the app runs its setup.exe{proton} in its own window. Choose the options you want there "
                    "(FitGirl lets you skip extra languages and bonus content) and click Install. When it closes, the "
                    "app finds the installed game and sets it up for Play.")
        cl.addWidget(label(text, None, wrap=True))
        if (e.setup or {}).get("last_result") and not installing:
            cl.addWidget(Banner("warning", e.setup["last_result"]))
        self._inst_box = QVBoxLayout()
        self._inst_box.setSpacing(4)
        cl.addLayout(self._inst_box)
        row = QHBoxLayout()
        if not installing:
            row.addWidget(button("Install game…", "download", "primary", on_click=lambda: self._install(e)))
            self._verify_btn = button("Check repack files", "check", "ghost",
                                      tooltip="Compare the data files with the repack's MD5 list (what its "
                                              "“Verify BIN files” script does)", on_click=lambda: self._verify(e))
            self._verify_btn.setEnabled(False)
            row.addWidget(self._verify_btn)
        row.addStretch(1)
        cl.addLayout(row)
        cl.addWidget(label("Nothing runs until you press Install and confirm. Only install repacks from sources you "
                           "trust.", "small", wrap=True))
        self.bridge.call(self.bridge.core.library_call("installer_info", e.id),
                         lambda info: gen == self._render_gen and self._installer_details(info), owner=self)
        return c

    def _installer_details(self, info: dict | None) -> None:
        from ezgd.core.fsutil import format_bytes
        if not info:
            return
        try:
            clear_layout(self._inst_box)
        except RuntimeError:
            return
        data = f"{info.get('data_files', 0)} data file(s), {format_bytes(info.get('data_bytes') or 0)}"
        rows = [("Installer", f"{Path(info.get('setup') or '').name} — {info.get('label', 'installer')} · {data}")]
        if info.get("target"):
            rows.append(("Installs to", f"{info['target']}  (preset — you can change it in the installer)"))
        if info.get("optional"):
            rows.append(("Optional parts", ", ".join(info["optional"][:6])))
        form = QFormLayout()
        for k, v in rows:
            form.addRow(label(k, "muted"), label(v, "small", wrap=True, selectable=True))
        self._inst_box.addLayout(form)
        if hasattr(self, "_verify_btn"):
            try:
                self._verify_btn.setEnabled(bool(info.get("md5")))
                if not info.get("md5"):
                    self._verify_btn.setToolTip("This repack has no MD5 list to check against.")
            except RuntimeError:
                pass

    def _repack_left(self, e, info: dict | None) -> None:
        from ezgd.core.fsutil import format_bytes
        if not info:
            return
        if info.get("state") == "pending" and not (e.setup or {}).get("state"):
            self.load()                          # turned out to be an installer: show the Install step
            return
        if info.get("state") != "installed" or not info.get("repack_left"):
            return
        b = Banner("info", f"The game is installed. The repack files ({format_bytes(info.get('repack_bytes') or 0)}) "
                           "in its download are no longer needed — keep them to reinstall later, or delete them.")
        b.add_action("Delete repack files…", lambda: self._delete_repack(e, info), "trash")
        try:
            self._advice_holder.addWidget(b)
        except RuntimeError:
            pass

    def _install(self, e) -> None:
        def go(info: dict | None) -> None:
            if not info or info.get("state") != "pending":
                self.actions.toasts.show("No installer found", "This game does not need installing.", "info")
                self.load()
                return
            dlg = QDialog(self)
            dlg.setWindowTitle(f"Install {e.title}")
            lay = QVBoxLayout(dlg)
            lay.addWidget(label(f"Run the installer for “{e.title}”?", "h2", wrap=True))
            lay.addWidget(label(info.get("setup", ""), "small", selectable=True, wrap=True))
            how = ("It runs with Proton in this game's own Windows prefix" if self.env.get("linux")
                   else "It opens as a normal Windows installer")
            lay.addWidget(label(f"{how}. In the installer, choose what to install and click Install; the folder is "
                                f"preset to:\n{info.get('target')}\nWhen it closes, the app finds the game and sets "
                                "it up for Play (you confirm once more on the first Play).", None, wrap=True))
            verify = QCheckBox("Check the repack files first (MD5) — recommended after a download")
            verify.setChecked(bool(info.get("md5")))
            verify.setEnabled(bool(info.get("md5")))
            lay.addWidget(verify)
            lay.addWidget(label("Only run installers from sources you trust.", "small", wrap=True))
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.button(QDialogButtonBox.Ok).setText("Install")
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            lay.addWidget(bb)
            if dlg.exec() != QDialog.Accepted:
                return
            if verify.isChecked():
                self._verify(e, then_install=True)
            else:
                self._run_installer(e)
        self.bridge.call(self.bridge.core.library_call("installer_info", e.id), go,
                         lambda err: self.actions.fail("Install", err))

    def _verify(self, e, then_install: bool = False) -> None:
        self.actions.toasts.show("Checking the repack files…", "This reads every data file once; big repacks take a "
                                                               "few minutes.", "info")
        if hasattr(self, "_verify_btn"):
            try:
                self._verify_btn.setEnabled(False)
                self._verify_btn.setText("Checking…")
            except RuntimeError:
                pass

        def done(r: dict) -> None:
            broken = (r.get("bad") or []) + (r.get("missing") or [])
            if not broken:
                self.actions.toasts.show("Repack files are OK", f"{r.get('checked', 0)} file(s) match the MD5 list.",
                                         "success")
                if then_install:
                    self._run_installer(e)
                else:
                    self.load()
                return
            names = ", ".join(broken[:6]) + ("…" if len(broken) > 6 else "")
            msg = (f"{len(r.get('bad') or [])} damaged and {len(r.get('missing') or [])} missing file(s): {names}\n\n"
                   "Installing would most likely fail part-way. For a torrent, use “Check files” on the download (it "
                   "re-downloads only the broken pieces); for file-host downloads, download those parts again.")
            if then_install:
                r2 = QMessageBox.warning(self, "Repack files damaged", msg + "\n\nInstall anyway?",
                                         QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
                if r2 == QMessageBox.Yes:
                    self._run_installer(e)
                    return
            else:
                QMessageBox.warning(self, "Repack files damaged", msg)
            self.load()
        self.bridge.call(self.bridge.core.library_call("verify_repack", e.id), done,
                         lambda err: (self.actions.fail("Check repack files", err), self.load()))

    def _run_installer(self, e) -> None:
        def ok(r: dict) -> None:
            self.actions.toasts.show("Installer started", "Finish it in its window — the app sets up the game when "
                                                          "it closes.", "success")
            self.load()
        self.bridge.call(self.bridge.core.library_call("run_installer", e.id, True), ok,
                         lambda err: self.actions.fail("Could not start the installer", err))

    def _delete_repack(self, e, info: dict) -> None:
        from ezgd.core.fsutil import format_bytes
        r = QMessageBox.question(self, "Delete repack files",
                                 f"Delete the repack files of “{e.title}” ({format_bytes(info.get('repack_bytes') or 0)})?"
                                 f"\n\n{info.get('root')}\n\nThe installed game is kept. You would need to download "
                                 "the repack again to reinstall it.")
        if r != QMessageBox.Yes:
            return
        self.bridge.call(self.bridge.core.library_call("delete_repack", e.id),
                         lambda freed: (self.actions.toasts.show("Repack files deleted", f"Freed {format_bytes(freed)}.",
                                                                 "success"), self.load()),
                         lambda err: self.actions.fail("Delete repack files", err))

    def _files_card(self, e) -> QFrame:
        c, cl = card()
        cl.addWidget(label("Files", "h2"))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        for name, path in (("Install folder", e.install_path), ("Download", e.download_path),
                           ("Executable", e.executable)):
            row = QHBoxLayout()
            row.addWidget(PathLabel(path or ""), 1)
            if path:
                row.addWidget(button("", "folder", "ghost", tooltip="Open folder",
                                     on_click=lambda _=False, p=path: open_folder(p)))
            form.addRow(label(name, "muted"), row)
        cl.addLayout(form)
        xrow = QHBoxLayout()
        self._archive_combo = QComboBox()
        self._archive_combo.setMinimumWidth(120)
        self._archive_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._archive_combo.setMinimumContentsLength(12)
        xrow.addWidget(self._archive_combo, 1)
        self._extract_btn = button("Extract…", "folder", on_click=lambda: self._extract(e))
        self._extract_btn.setEnabled(False)
        xrow.addWidget(self._extract_btn)
        cl.addLayout(xrow)
        self._archive_note = label("Looking for archives…", "small", wrap=True)
        cl.addWidget(self._archive_note)
        self.bridge.call(self.bridge.core.library_archives(e.id), self._archives, owner=self)
        urow = QHBoxLayout()
        urow.addStretch(1)
        urow.addWidget(button("Uninstall…", "trash", "danger",
                              tooltip="Delete the game's files, downloads and Proton prefix, and remove it from "
                                      "the Library — you choose what goes",
                              on_click=lambda: self._uninstall(e)))
        cl.addLayout(urow)
        # launch files: every executable / release script found, what it is, and which one Play uses
        cl.addWidget(label("Launch files", "h2"))
        self._launch_box = QVBoxLayout()
        self._launch_box.setSpacing(4)
        self._launch_box.addWidget(label("Looking for executables…", "small"))
        cl.addLayout(self._launch_box)
        self._launch_entry = e
        # kept for compatibility with callers that pick an executable from the page
        self._exe_combo = QComboBox()
        self._exe_combo.hide()
        cl.addWidget(self._exe_combo)
        if e.install_path or e.download_path:
            self.bridge.call(self.bridge.core.library_call("candidates", e.id), self._candidates, owner=self)
        return c

    def _show_suggestions(self, e, items: list[dict]) -> None:
        if not items:
            return
        try:
            c, cl = card()
        except RuntimeError:
            return
        head = QHBoxLayout()
        head.addWidget(label("Suggested fixes", "h2"), 1)
        head.addWidget(button("Open all fixes…", "wrench", on_click=lambda: self._open_fixes(e)))
        cl.addLayout(head)
        cl.addWidget(label("Found by checking this game's files and its own logs. Every fix can be switched on "
                           "or off later on the Fixes screen.", "small", wrap=True))
        for s in items:
            b = Banner(s["severity"], f"{s['title']}\n{s['detail']}")
            if s["changes"]:
                b.add_action("Apply", lambda k=s["key"], t=s["title"]: self._apply_suggestion(e, k, t), "check")
            cl.addWidget(b)
        self._advice_holder.addWidget(c)

    def _fix_count(self, items: list[dict]) -> None:
        pending = sum(1 for i in items if i["recommended"] and not i["enabled"])
        on = sum(1 for i in items if i["enabled"] and i["key"] not in ("protonfixes", "runtime"))
        try:
            if pending:
                self._fixes_btn.setText(f"  Fixes ({pending} recommended)")
                self._fixes_btn.setProperty("variant", "setup")
            else:
                self._fixes_btn.setText(f"  Fixes ({on} on)" if on else "  Fixes")
            self._fixes_btn.style().unpolish(self._fixes_btn)
            self._fixes_btn.style().polish(self._fixes_btn)
        except RuntimeError:
            pass

    def _open_fixes(self, e) -> None:
        from ezgd.ui.dialogs.fixes import open_fixes
        if open_fixes(self.actions, self.entries.get(e.id, e), self):
            self.actions.toasts.show("Fixes saved", "They are used the next time you press Play.", "success")
            self.load()

    def _apply_suggestion(self, e, key: str, title: str) -> None:
        self.bridge.call(self.bridge.core.library_call("apply_suggestion", e.id, key),
                         lambda _r: (self.actions.toasts.show("Fix applied", title, "success"), self.load()),
                         lambda err: self.actions.fail("Could not apply", err))

    def _show_password(self, source: str | None, password: str | None) -> None:
        self._password = password
        if not password:
            return
        e = self.entries.get(self.current_id)
        try:
            if e is not None and e.status is LibraryStatus.INSTALLED:
                # installed: the app already used it — a quiet line instead of a big banner
                row = QHBoxLayout()
                row.addWidget(label(f"Archive password: {password}", "small", selectable=True))
                row.addWidget(button("Copy", "link", "ghost", on_click=lambda: QGuiApplication.clipboard().setText(
                    password)))
                row.addStretch(1)
                self._password_holder.addLayout(row)
            else:
                self._password_holder.addWidget(PasswordBanner(source, password))
        except RuntimeError:
            pass

    def _dupes(self, dupes: list) -> None:
        try:
            clear_layout(self._dupe_holder)
        except RuntimeError:
            return
        if dupes:
            names = ", ".join(f"{d.title} ({STATUS_TEXT.get(str(d.status), d.status)})" for d in dupes[:3])
            self._dupe_holder.addWidget(Banner("warning", f"Possible duplicate installation: {names}."))

    def _archives(self, archives: list[str]) -> None:
        try:
            self._archive_combo.clear()
        except RuntimeError:
            return
        for a in archives:
            self._archive_combo.addItem(Path(a).name, a)
        self._extract_btn.setEnabled(bool(archives))
        self._archive_note.setText("Archives are extracted safely; nested archives are never extracted "
                                   "automatically — extract them here if a game needs it."
                                   if archives else "No archives found in this game's files.")

    def _candidates(self, cands: list) -> None:
        try:
            self._exe_combo.clear()
        except RuntimeError:
            return
        for c in cands[:25]:
            self._exe_combo.addItem(c.label, str(c.path))
            self._exe_combo.setItemData(self._exe_combo.count() - 1, c.details, Qt.ToolTipRole)
        self._show_launch_files(cands)

    def _show_launch_files(self, cands: list) -> None:
        clear_layout(self._launch_box)
        e = self._launch_entry
        if not cands:
            self._launch_box.addWidget(label("No executables found in this game's folder. Use Properties → "
                                             "Browse… to pick one.", "small", wrap=True))
            return
        games = [c for c in cands if c.kind not in ("tool", "installer")]
        shown = (games or cands)[:8]
        if e.executable and not any(str(c.path) == e.executable for c in shown):
            shown += [c for c in cands if str(c.path) == e.executable]
        for c in shown:
            row = QHBoxLayout()
            row.setSpacing(6)
            name = label(c.path.name)
            name.setStyleSheet("font-weight: 600;")
            name.setToolTip(c.details)
            row.addWidget(name)
            for m in c.modes:
                row.addWidget(chip(m, theme.CURRENT.accent))
            if c.kind == "launch script":
                row.addWidget(chip(f"Release script → {c.starts}", theme.CURRENT.success))
            elif c.kind == "tool":
                row.addWidget(chip("Probably not the game", theme.CURRENT.warning))
            elif c.kind == "installer":
                row.addWidget(chip("Installer", theme.CURRENT.accent))
            row.addStretch(1)
            if str(c.path) == e.executable:
                row.addWidget(chip("Used by Play", theme.CURRENT.success))
            else:
                row.addWidget(button("Use this", variant="ghost", tooltip=c.details,
                                     on_click=lambda _=False, p=str(c.path): self._call(
                                         "set_executable", e.id, p, ok_text="Executable changed")))
            self._launch_box.addLayout(row)
            why = label(c.reason + (f" · first runs: {' · '.join(c.prepares[:3])}" if c.prepares else ""),
                        "small", wrap=True)
            why.setContentsMargins(0, 0, 0, 4)
            self._launch_box.addWidget(why)
        hidden = len(cands) - len(shown)
        if hidden > 0:
            self._launch_box.addWidget(label(f"{hidden} more (tools, installers, helpers) in Properties → "
                                             "Executable.", "small"))

    # -------------------------------------------------------------- actions
    def _call(self, method: str, *args, ok_text: str | None = None) -> None:
        def ok(_res):
            if ok_text:
                self.actions.toasts.show(ok_text, kind="success")
            self.load()
        self.bridge.call(self.bridge.core.library_call(method, *args), ok,
                         lambda err: self.actions.fail("Library", err))

    def _properties(self, e, tab: str = "general") -> None:
        if open_properties(self.actions, self.entries.get(e.id, e), self.env, self, tab):
            self.actions.toasts.show("Properties saved", kind="success")
            self.load()

    def _stop(self, e) -> None:
        r = QMessageBox.question(self, "Stop game", f"Force-stop {e.title}? Unsaved progress is lost.")
        if r == QMessageBox.Yes:
            self._call("stop", e.id, ok_text="Game stopped")

    def _show_check(self, e, st: dict) -> None:
        """The new-game check (started once, out of sight, before the first Play)."""
        status = st.get("status")
        texts = {"waiting": ("info", "This game is checked once, out of sight, as soon as no game is running."),
                 "checking": ("info", "Checking this game out of sight: " + (st.get("detail") or "") + "…"),
                 "ready": ("success", "Checked before its first start: " + (st.get("detail") or "it works") + "."),
                 "fixed": ("success", "Its first start failed — fixed before you played: " + (st.get("detail") or "")
                           + "."),
                 "problem": ("warning", "Checked before its first start — a problem the app could not fix: "
                             + (st.get("detail") or "") + " (Diagnose has the details.)")}
        if status not in texts:
            return
        kind, text = texts[status]
        b = Banner(kind, text)
        if st.get("report"):
            from pathlib import Path as _P
            from ezgd.ui.dialogs.diagnose import _open_file
            rep = _P(st["report"])
            b.actions.addWidget(button("Report", "folder", on_click=lambda: _open_file(rep)))
        self._check_holder.addWidget(b)

    def _froze(self, e) -> None:
        """The game hangs: what its threads are doing is read first (the debugging assistant's evidence for a
        freeze), then it is stopped."""
        self.bridge.call(self.bridge.core.report_freeze(e.id),
                         lambda verdict: self.actions.toasts.show(f"{e.title} stopped", f"Kept what it was doing: "
                                                                   f"{verdict} Diagnose → Fix my game starts from it.",
                                                                   "success"),
                         lambda err: self.actions.fail("Could not stop the game", err), owner=self)

    def _resume(self, e) -> None:
        """A suspended game: let it run again and give it the screen."""
        def done(_r) -> None:
            from ezgd.ui.window_control import bring_game_forward
            self.bridge.call(self.bridge.core.library_call("game_processes", e.id), bring_game_forward,
                             lambda _err: None, owner=self)
        self.bridge.call(self.bridge.core.library_call("resume", e.id), done,
                         lambda err: self.actions.fail("Could not resume the game", err), owner=self)

    def _suspend(self, e) -> None:
        self.bridge.call(self.bridge.core.library_call("suspend", e.id),
                         lambda _r: self.actions.toasts.show(f"{e.title} is suspended", "It uses no processor or "
                                                             "graphics time and continues where it was: Resume.",
                                                             "success"),
                         lambda err: self.actions.fail("Could not suspend the game", err), owner=self)

    def _more_menu(self, e, anchor) -> None:
        m = QMenu(self)
        if e.id in self.suspended:
            m.addAction("Resume the game", lambda: self._resume(e))
        elif e.id in self.running:
            m.addAction("Suspend (freeze) the game", lambda: self._suspend(e))
            m.addAction("It froze — stop it and keep what happened", lambda: self._froze(e))
        self._organize_menu(m, e)
        m.addSeparator()
        m.addAction("Fixes…", lambda: self._open_fixes(e)).setEnabled(bool(e.executable))
        m.addAction("Diagnose (why doesn't it work?)…", lambda: self._diagnose(e)).setEnabled(bool(e.executable))
        m.addAction("Properties…", lambda: self._properties(e))
        m.addAction("Compatibility (Proton)…", lambda: self._properties(e, "compat")).setEnabled(
            bool(self.env.get("linux")))
        m.addSeparator()
        if e.status is not LibraryStatus.INSTALLED:
            m.addAction("Mark as installed", lambda: self._mark_installed(e))
        m.addAction("Change install folder…", lambda: self._change_install(e))
        if self.env.get("steam_root") and e.executable:
            m.addAction("Add to Steam as a non-Steam game…", lambda: self._add_to_steam(e))
        if self.env.get("linux") and e.executable and not e.executable.lower().endswith((".exe", ".bat", ".msi")) \
                and not os.access(e.executable, os.X_OK):
            m.addAction("Allow executing this file…", lambda: self._allow_exec(e))
        log_path = self.bridge.core.paths.logs / f"game-{e.id}.log"
        if log_path.exists():
            m.addAction("Open game log folder", lambda: open_folder(log_path))
        m.addSeparator()
        m.addAction("Uninstall…", lambda: self._uninstall(e))
        m.addAction("Remove from Library (keep files)…", lambda: self._remove(e))
        m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _mark_installed(self, e) -> None:
        path = e.install_path
        if not path:
            path = QFileDialog.getExistingDirectory(self, "Choose the installation folder",
                                                    e.download_path or str(Path.home()))
            if not path:
                return
        exe = self._exe_combo.currentData() if not e.executable else None
        self._call("mark_installed", e.id, path, exe, ok_text="Marked as installed")

    def _change_install(self, e) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose the installation folder",
                                             e.install_path or e.download_path or str(Path.home()))
        if d:
            self.bridge.call(self.bridge.core.library_call("update", e.id, install_path=d),
                             lambda _r: self.load(), lambda err: self.actions.fail("Library", err))

    def _extract(self, e) -> None:
        archive = self._archive_combo.currentData()
        if not archive:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Extract archive")
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        dest = QLineEdit(e.install_path or str(Path(self.actions.settings.get("install_dir", "")) / e.title))
        from ezgd.ui.dialogs.download_options import folder_row
        form.addRow("Extract to", folder_row(dest, dlg, "Choose extraction folder"))
        pw = QLineEdit(self._password or "")
        pw.setPlaceholderText("no password known — leave empty if the archive is not protected")
        form.addRow("Password", pw)
        if self._password:
            form.addRow("", label(f"Filled in with the password published by {theme.source_name(e.source)}.",
                                  "small", wrap=True))
        lay.addWidget(label(Path(archive).name, "h2"))
        lay.addLayout(form)
        lay.addWidget(label("Files are unpacked to a temporary folder first and only moved into place if the whole "
                            "archive is valid. Paths that would escape the destination are rejected.", "small",
                            wrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        self.bridge.call(self.bridge.core.extract_entry(e.id, archive, dest.text().strip() or None,
                                                        pw.text() or None),
                         lambda ok: self.load(), lambda err: self.actions.fail("Extraction", err))
        # progress arrives through "extraction" events
        self.extraction_state[e.id] = {"id": None, "archive": Path(archive).name, "progress": 0.0,
                                       "status": "running"}
        self.load()

    def _add_to_steam(self, e) -> None:
        users = self.env.get("steam_users", [])
        user = None
        if len(users) > 1:
            user, ok = QInputDialog.getItem(self, "Steam account", "Add the shortcut for which Steam account?",
                                            users, 0, False)
            if not ok:
                return
        r = QMessageBox.question(self, "Add to Steam",
                                 "This adds a non-Steam game shortcut to Steam's shortcuts.vdf (a backup is kept).\n\n"
                                 "Close Steam first — Steam overwrites the file when it exits. Continue?")
        if r != QMessageBox.Yes:
            return
        self._call("add_to_steam", e.id, user, ok_text="Added to Steam — restart Steam to see it")

    def _allow_exec(self, e) -> None:
        r = QMessageBox.warning(self, "Allow executing",
                                f"Mark this file as executable?\n\n{e.executable}\n\nOnly do this for software you "
                                f"trust.", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r == QMessageBox.Yes:
            self._call("allow_execute", e.id, ok_text="File marked as executable")

    def _diagnose(self, e) -> None:
        from ezgd.ui.dialogs.diagnose import open_diagnose
        open_diagnose(self.actions, self.entries.get(e.id, e), self)
        self.load()

    def _leftovers(self) -> None:
        from ezgd.ui.dialogs.uninstall import open_leftovers
        open_leftovers(self.actions, self)

    def _uninstall(self, e) -> None:
        from ezgd.core.fsutil import format_bytes
        from ezgd.ui.dialogs.uninstall import open_uninstall
        r = open_uninstall(self.actions, self.entries.get(e.id, e), self)
        if r is None:
            self.load()                           # an incomplete uninstall updates the entry's notes
            return
        self.current_id = None
        text = f"Freed {format_bytes(r['freed'])}." if r["freed"] else "Removed from your Library."
        if r["kept"]:
            text += f" {len(r['kept'])} item(s) were left in place (outside the app's folders or shared)."
        self.actions.toasts.show(f"{r['title']} uninstalled", text, "success")
        self.load()

    def _remove(self, e) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Remove from Library")
        lay = QVBoxLayout(dlg)
        lay.addWidget(label(f"Remove “{e.title}” from the Library?", "h2", wrap=True))
        lay.addWidget(label("To delete everything the app installed for it (game files, downloads, Proton prefix, "
                            "Steam shortcut), use Uninstall… instead.", "small", wrap=True))
        delete = QCheckBox("Also permanently delete its files from disk")
        lay.addWidget(delete)
        paths = [p for p in (e.install_path, e.download_path) if p]
        if paths:
            lay.addWidget(label("\n".join(paths), "small", selectable=True, wrap=True))
        lay.addWidget(label("Files are only deleted if they are inside your configured download or installation "
                            "folders. The Proton prefix (saves stored in it) is kept.", "small", wrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Remove")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        if delete.isChecked():
            r = QMessageBox.warning(self, "Delete files", "This cannot be undone. Delete the files now?",
                                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return
        self.current_id = None
        self._call("remove", e.id, delete.isChecked(), ok_text="Removed from library")

    def import_game(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Import game")
        box.setText("What do you want to import?")
        folder_btn = box.addButton("A game folder", QMessageBox.AcceptRole)
        file_btn = box.addButton("An archive / file", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is folder_btn:
            path = QFileDialog.getExistingDirectory(self, "Choose the game folder", self._import_start())
        elif box.clickedButton() is file_btn:
            path, _ = QFileDialog.getOpenFileName(self, "Choose the archive", self._import_start(),
                                                  "Archives (*.zip *.7z *.rar *.tar *.gz *.xz *.bz2 *.001);;All files (*)")
        else:
            return
        if path:
            self.import_path(path)

    def _import_start(self) -> str:
        """Where the import dialogs open: the Downloads folder (where browsers put files), else home."""
        for p in (Path.home() / "Downloads", Path(self.actions.settings.get("download_dir") or "")):
            if str(p) and p.is_dir():
                return str(p)
        return str(Path.home())

    def import_path(self, path: str) -> None:
        """Import a game folder or archive (also what is dropped on the window), asking only for its title."""
        from ezgd.ui.dropin import guess_title
        title, ok = QInputDialog.getText(self, "Import game", f"Title for “{Path(path).name}”:",
                                         text=guess_title(path))
        if not (ok and title.strip()):
            return

        def imported(entry):
            self.actions.toasts.show("Imported into the library", entry.title, "success")
            self.current_id = entry.id
            self.load()
            # look up the Steam App ID in the background so Proton can apply per-game fixes
            self.bridge.call(self.bridge.core.lookup_steam_app_id(entry.id), lambda _r: self.load(),
                             lambda _e: None, owner=self)
        self.bridge.call(self.bridge.core.library_call("import_folder", title.strip(), path), imported,
                         lambda err: self.actions.fail("Import", err))
