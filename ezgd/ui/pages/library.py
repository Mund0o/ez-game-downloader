"""Library: calm, modern game hub with a guided Setup Studio, custom sidebar cards, and tabbed details."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QImage, QKeySequence,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QToolButton,
                               QVBoxLayout, QWidget)

from ezgd.core.fsutil import format_bytes
from ezgd.library.models import LibraryStatus
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_folder
from ezgd.ui.dialogs.properties import open_properties
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import (Banner, EmptyState, FlowLayout, PasswordBanner, PathLabel, button, card, chip,
                                    hline, label, relative_time, source_chip, tool)
from ezgd.ui.widgets.showcase import ElidedLabel, _SpotlightStage, clear_now

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


def _initials(title: str) -> str:
    return "".join(x[0] for x in title.split()[:2] if x).upper() or "?"


class _HeroCover(QWidget):
    """Crisp rounded 16:9 cover artwork (preserving portrait covers cleanly, or stylized initials when absent)."""

    def __init__(self, covers, parent: QWidget | None = None):
        super().__init__(parent)
        self.covers = covers
        self._entry = None
        self.setFixedSize(268, 151)

    def set_entry(self, entry) -> None:
        self._entry = entry
        self.update()

    def paintEvent(self, _e) -> None:
        pal = theme.CURRENT
        w, h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        e = self._entry
        url = getattr(e, "cover_url", None) if e is not None else None
        pm = (self.covers.variant(url, ("lib_hero", url, w, h, dpr),
                                  lambda img, w=w, h=h, dpr=dpr: _SpotlightStage._finish_cover(img, w, h, dpr))
              if url else None)

        rect = QRectF(0, 0, w, h)
        if pm is not None:
            p.drawPixmap(QPointF(0, 0), pm)
        else:
            clip = QPainterPath()
            clip.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 13, 13)
            p.save()
            p.setClipPath(clip)
            src_hex = theme.SOURCE_COLORS.get(getattr(e, "source", None) or "", pal.accent)
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, QColor(src_hex).darker(235))
            grad.setColorAt(1.0, QColor(pal.bg3))
            p.fillRect(rect, QBrush(grad))
            f = QFont(self.font())
            f.setPointSize(26)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 76))
            p.drawText(rect, Qt.AlignCenter, _initials(getattr(e, "title", "") or "?"))
            p.restore()

        p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 13, 13)
        p.end()


class _LibraryItemDelegate(QStyledItemDelegate):
    """Paints sleek modern sidebar game rows with rounded 16:9 thumbnails, title, favorite star, and status dot."""

    ROW_H = 54

    def __init__(self, page: "LibraryPage"):
        super().__init__(page.list)
        self.page = page

    @staticmethod
    def _finish_thumb(img: QImage, w: int, h: int, dpr: float) -> QImage:
        pw, ph = max(1, round(w * dpr)), max(1, round(h * dpr))
        out = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
        out.fill(Qt.transparent)
        q = QPainter(out)
        q.setRenderHint(QPainter.Antialiasing)
        q.setRenderHint(QPainter.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, pw, ph), 6 * dpr, 6 * dpr)
        q.setClipPath(clip)
        scaled = img.scaled(QSize(pw, ph), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        q.drawImage(0, 0, scaled, (scaled.width() - pw) // 2, (scaled.height() - ph) // 2, pw, ph)
        q.end()
        out.setDevicePixelRatio(dpr)
        return out

    def sizeHint(self, _option, _index) -> QSize:       # noqa: N802
        return QSize(180, self.ROW_H)

    def paint(self, p: QPainter, option, index) -> None:
        eid = index.data(Qt.UserRole)
        e = self.page.entries.get(eid)
        if e is None:
            return
        pal = theme.CURRENT
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        r = QRectF(option.rect).adjusted(2, 2, -4, -2)
        dpr = p.device().devicePixelRatioF() if p.device() is not None else 1.0

        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        if selected:
            fill = QColor(pal.accent)
            fill.setAlpha(42)
            border = QColor(pal.accent)
            border.setAlpha(150)
            p.setBrush(fill)
            p.setPen(QPen(border, 1.0))
            p.drawRoundedRect(r, 9, 9)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.accent))
            p.drawRoundedRect(QRectF(r.left() + 1, r.top() + 10, 3.0, r.height() - 20), 1.5, 1.5)
        elif hovered:
            p.setBrush(QColor(pal.bg2))
            p.setPen(QPen(QColor(pal.border), 1.0))
            p.drawRoundedRect(r, 9, 9)

        # Rounded 16:9 thumbnail
        tw, th = 62, 35
        tx = r.left() + 9
        ty = r.center().y() - th / 2.0
        trect = QRectF(tx, ty, tw, th)
        pm = (self.page.actions.covers.variant(
            e.cover_url, ("lib_row", e.cover_url, tw, th, dpr),
            lambda img, tw=tw, th=th, dpr=dpr: self._finish_thumb(img, tw, th, dpr))
            if e.cover_url else None)
        if pm is not None:
            p.drawPixmap(QPointF(tx, ty), pm)
        else:
            cpath = QPainterPath()
            cpath.addRoundedRect(trect, 6, 6)
            p.save()
            p.setClipPath(cpath)
            src_hex = theme.SOURCE_COLORS.get(e.source or "", pal.accent)
            grad = QLinearGradient(trect.topLeft(), trect.bottomRight())
            grad.setColorAt(0.0, QColor(src_hex).darker(230))
            grad.setColorAt(1.0, QColor(pal.bg3))
            p.fillRect(trect, QBrush(grad))
            ifont = QFont(option.font)
            ifont.setPointSizeF(8.8)
            ifont.setBold(True)
            p.setFont(ifont)
            p.setPen(QColor(255, 255, 255, 130))
            p.drawText(trect, Qt.AlignCenter, _initials(e.title))
            p.restore()

        # Title + optional favorite star
        rx = trect.right() + 10
        rw = max(40.0, r.right() - rx - 10)
        star_w = 16.0 if e.favorite else 0.0
        tf = QFont(option.font)
        tf.setPointSizeF(9.6)
        tf.setWeight(QFont.DemiBold if not selected else QFont.Bold)
        p.setFont(tf)
        tfm = QFontMetrics(tf)
        p.setPen(QColor("#ffffff" if selected else pal.text))
        title_str = tfm.elidedText(e.title, Qt.ElideRight, int(rw - star_w))
        p.drawText(QRectF(rx, r.top() + 6, rw - star_w, 20), Qt.AlignLeft | Qt.AlignVCenter, title_str)
        if e.favorite:
            p.setPen(QColor("#f59e0b"))
            p.drawText(QRectF(r.right() - 22, r.top() + 6, 14, 20), Qt.AlignRight | Qt.AlignVCenter, "★")

        # Status dot + subtitle
        status_str = self.page._status_text(e)
        dot_col = self.page._entry_status_color(e)
        dy = r.bottom() - 18
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(dot_col))
        p.drawEllipse(QRectF(rx, dy + 5.5, 6.0, 6.0))

        sf = QFont(option.font)
        sf.setPointSizeF(8.1)
        p.setFont(sf)
        sfm = QFontMetrics(sf)
        src = theme.source_name(e.source) if e.source else "Imported"
        sub = f"{status_str}  ·  {src}"
        p.setPen(QColor(pal.accent_hover if (eid in self.page.running) else pal.muted))
        p.drawText(QRectF(rx + 11, dy, rw - 11, 17), Qt.AlignLeft | Qt.AlignVCenter,
                   sfm.elidedText(sub, Qt.ElideRight, int(rw - 11)))

        p.restore()


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
        self._detail_tab: str | None = None      # sticky user-chosen tab ("overview" | "setup" | "files"), or auto
        actions.covers.ready.connect(self._cover_ready)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 6, 24, 16)
        lay.setSpacing(12)

        # Clean, calm top header bar
        head_bar = QFrame()
        head_bar.setObjectName("card")
        head_lay = QHBoxLayout(head_bar)
        head_lay.setContentsMargins(16, 10, 16, 10)
        head_lay.setSpacing(10)
        lib_ic = label()
        lib_ic.setPixmap(icon("library", theme.CURRENT.accent_hover, 22).pixmap(22, 22))
        head_lay.addWidget(lib_ic, 0, Qt.AlignVCenter)
        title_lbl = label("Library")
        title_lbl.setStyleSheet("font-size: 15.5pt; font-weight: 750;")
        head_lay.addWidget(title_lbl, 0, Qt.AlignVCenter)
        self.summary = label("", "muted")
        head_lay.addWidget(self.summary, 1, Qt.AlignVCenter)

        head_lay.addWidget(button("Check updates", "update", "ghost",
                                  tooltip="Look at every installed game's page for a newer version now",
                                  on_click=self._check_all_updates))
        tools_btn = button("Manage", "disk", "ghost", tooltip="Storage usage & leftover file cleanup")
        tools_btn.clicked.connect(lambda _=False, b=tools_btn: self._library_tools_menu(b))
        head_lay.addWidget(tools_btn)
        head_lay.addWidget(button("Import game", "plus", "primary", on_click=self.import_game))
        lay.addWidget(head_bar)

        # Two-pane workspace: sleek sidebar list + calm detail stage
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(1)

        left = QFrame()
        left.setObjectName("card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        ll.setSpacing(8)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter library…")
        self.filter.setTextMargins(22, 0, 0, 0)
        self._filter_ic = QLabel(self.filter)
        self._filter_ic.setPixmap(icon("search", theme.CURRENT.muted, 15).pixmap(15, 15))
        self._filter_ic.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.filter.installEventFilter(self)
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
        self.list.setItemDelegate(_LibraryItemDelegate(self))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { background: transparent; border: none; padding: 0px; }"
            "QListWidget::item:selected, QListWidget::item:hover { background: transparent; }"
        )
        self.list.setSpacing(2)
        self.list.currentItemChanged.connect(self._selected)
        self._pending_id = None
        self._select_timer = QTimer(self)
        self._select_timer.setSingleShot(True)
        self._select_timer.setInterval(120)
        self._select_timer.timeout.connect(self._render_pending)
        self.list.itemDoubleClicked.connect(self._double_clicked)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        enter = QShortcut(QKeySequence(Qt.Key_Return), self.list, context=Qt.WidgetShortcut)
        enter.activated.connect(lambda: self.list.currentItem() and self._double_clicked(self.list.currentItem()))
        self.list.setToolTip("Double-click or press Enter on a ready game to play it")
        ll.addWidget(self.list, 1)
        split.addWidget(left)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.detail = QWidget()
        self.dl = QVBoxLayout(self.detail)
        self.dl.setContentsMargins(14, 0, 4, 14)
        self.dl.setSpacing(12)
        self.detail_scroll.setWidget(self.detail)
        split.addWidget(self.detail_scroll)
        split.setSizes([296, 840])
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        left.setMinimumWidth(228)
        self.detail_scroll.viewport().installEventFilter(self)
        lay.addWidget(split, 1)
        self._place_filter_icon()
        self._show_empty()

    def _place_filter_icon(self) -> None:
        self._filter_ic.move(9, max(6, (self.filter.height() - 15) // 2))

    def _library_tools_menu(self, anchor: QWidget) -> None:
        m = QMenu(self)
        m.addAction(icon("disk", theme.CURRENT.text), "Storage usage…", self._storage)
        m.addAction(icon("trash", theme.CURRENT.text), "Clean up leftover files…", self._leftovers)
        m.addSeparator()
        m.addAction(icon("update", theme.CURRENT.text), "Check all games for updates", self._check_all_updates)
        m.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

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
            it.setSizeHint(QSize(180, _LibraryItemDelegate.ROW_H))
            self.list.addItem(it)
            if e.id == keep:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        self._refresh_items()

        visible_entries = [e for e in entries if not e.hidden]
        ready = sum(1 for e in visible_entries if e.status is LibraryStatus.INSTALLED and e.executable)
        setup_cnt = sum(1 for e in visible_entries if not (e.status is LibraryStatus.INSTALLED and e.executable))
        total_play = sum(e.playtime_seconds for e in entries)
        parts = [f"{len(visible_entries)} game{'s' if len(visible_entries) != 1 else ''}", f"{ready} ready"]
        if setup_cnt:
            parts.append(f"{setup_cnt} awaiting setup")
        if total_play >= 60:
            parts.append(f"{playtime_text(total_play)} played")
        self.summary.setText("  ·  ".join(parts))

        self._apply_filter()
        if keep in self.entries:
            self._render(keep)
        elif entries:
            self.list.setCurrentRow(0)
        else:
            self._show_empty()

    def _entry_needs_setup(self, e) -> bool:
        state = (e.setup or {}).get("state")
        if state in ("pending", "installing", "waiting_steam"):
            return True
        if e.status in (LibraryStatus.EXTRACTING, LibraryStatus.DOWNLOADED, LibraryStatus.EXTRACTED):
            return True
        if not e.executable:
            return True
        return False

    def _entry_status_color(self, e) -> str:
        if e.id in self.suspended:
            return theme.CURRENT.warning
        if e.id in self.running:
            return theme.CURRENT.success
        state = (e.setup or {}).get("state")
        if state in ("pending", "waiting_steam"):
            return theme.CURRENT.accent
        if state == "installing" or e.status is LibraryStatus.EXTRACTING:
            return theme.CURRENT.info
        if e.status is LibraryStatus.BROKEN:
            return theme.CURRENT.danger
        if not e.executable or e.status is not LibraryStatus.INSTALLED:
            return theme.CURRENT.warning
        return theme.CURRENT.success

    def _refresh_items(self) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            e = self.entries.get(it.data(Qt.UserRole))
            if e is None:
                continue
            status = self._status_text(e)
            src = theme.source_name(e.source) if e.source else "Imported"
            # Kept in item.text() for keyboard search & test assertions; painted visually by _LibraryItemDelegate
            it.setText(f"{'★ ' if e.favorite else ''}{e.title}\n{status} · {src}")
        self.list.viewport().update()

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
        if not e.executable and e.status is LibraryStatus.INSTALLED:
            return "Needs setup"
        return STATUS_TEXT.get(str(e.status), str(e.status))

    def _cover_ready(self, url: str) -> None:
        if any(e.cover_url == url for e in self.entries.values()):
            self.list.viewport().update()
            if self.current_id and self.entries.get(self.current_id) and \
                    self.entries[self.current_id].cover_url == url:
                cover = getattr(self, "_hero_cover", None)
                if cover is not None:
                    try:
                        cover.update()
                    except RuntimeError:
                        pass

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
                ok = False
            if kind == "favorites":
                ok = ok and e.favorite
            elif isinstance(kind, str) and kind.startswith("c:"):
                ok = ok and kind[2:] in e.collections
            elif kind == "ready":
                ok = ok and e.status is LibraryStatus.INSTALLED and bool(e.executable)
            elif kind == "setup":
                ok = ok and self._entry_needs_setup(e)
            elif kind == "running":
                ok = ok and e.id in self.running
            it.setHidden(not ok)

    def _fill_kinds(self, entries) -> None:
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
        if item is None:
            return
        new_id = item.data(Qt.UserRole)
        if new_id != self.current_id:
            self._detail_tab = None              # auto-pick Overview vs Setup for the newly selected game
        now = time.monotonic()
        busy = now - getattr(self, "_last_select", 0.0) < 0.2
        self._last_select = now
        self._pending_id = new_id
        if not busy:
            self._select_timer.stop()
            self._render(self._pending_id)
        else:
            self._select_timer.start()

    def _render_pending(self) -> None:
        if self._pending_id is not None:
            self._render(self._pending_id)

    def _show_empty(self) -> None:
        clear_now(self.dl)
        self.dl.addStretch(1)
        e = EmptyState("library", "Your library is empty",
                       "Every game you download appears here automatically, ready to set up and play. You can also "
                       "import a game folder or an archive you downloaded in your browser.")
        e.buttons.addWidget(button("Import game…", "plus", "primary", on_click=self.import_game))
        e.buttons.addWidget(button("Browse games", "grid", on_click=lambda: self.actions.navigate.emit("games")))
        self.dl.addWidget(e)
        self.dl.addStretch(1)

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
                self._xlabel.setText(f"Extracting {state['archive']} — {state['progress'] * 100:.0f}%  "
                                     f"{state.get('item', '')[:60]}")
            except RuntimeError:
                pass

    def game_state(self, payload: dict) -> None:
        gid = payload["id"]
        if "suspended" in payload:
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
        if self.current_id != entry_id:
            self._detail_tab = None
        self.current_id = entry_id
        self._render_gen = getattr(self, "_render_gen", 0) + 1
        gen = self._render_gen
        clear_now(self.dl)

        running = e.id in self.running
        needs_setup = self._entry_needs_setup(e)

        # 1. Clean Hero Showcase Card
        self.dl.addWidget(self._build_hero(e, running, needs_setup, gen))

        # 2. Quiet, Action-Only Alert Strip (0px when nothing needs attention!)
        self._alert_box = QVBoxLayout()
        self._alert_box.setSpacing(8)
        self.dl.addLayout(self._alert_box)

        if e.status is LibraryStatus.BROKEN:
            b = Banner("error", "Game files were not found at the saved folder. Locate the moved folder or remove it.")
            b.add_action("Locate folder…", lambda: self._change_install(e), "folder")
            self._alert_box.addWidget(b)

        if e.status is not LibraryStatus.EXTRACTING:
            self.bridge.call(self.bridge.core.interrupted_install(e.id),
                             lambda info: gen == self._render_gen and self._show_interrupted(e, info), owner=self)

        rel = e.release or {}
        if e.game_id and (rel.get("updating") or rel.get("status") in ("update", "different")):
            self._alert_box.addWidget(self._actionable_update_banner(e))

        self.bridge.call(self.bridge.core.library_call("duplicates", e.id),
                         lambda d: gen == self._render_gen and self._dupes(d), owner=self)

        if e.executable and not running and e.status is LibraryStatus.INSTALLED:
            self.bridge.call(self.bridge.core.library_call("last_run_status", e.id),
                             lambda st: gen == self._render_gen and self._last_run_failed(e, st), owner=self)
            self.bridge.call(self.bridge.core.preflight(e.id),
                             lambda items: gen == self._render_gen and self._show_preflight(e, items), owner=self)
        if e.executable:
            self.bridge.call(self.bridge.core.library_call("suggestions", e.id),
                             lambda s: gen == self._render_gen and self._show_suggestions(e, s), owner=self)

        if (e.setup or {}).get("state") == "installed" or (not e.setup and e.status is not LibraryStatus.INSTALLED):
            self.bridge.call(self.bridge.core.library_call("installer_info", e.id),
                             lambda info: gen == self._render_gen and self._repack_left(e, info), owner=self)

        # 3. Segmented Sub-Navigation Pills + Adaptive Stack
        if needs_setup:
            # While setting up / extracting / installing: keep Setup front-and-center, with Files & Saves 1 click away
            default_tab = "setup"
        else:
            default_tab = "overview"
        chosen_tab = self._detail_tab if self._detail_tab in ("overview", "setup", "files") else default_tab

        tab_bar = QHBoxLayout()
        tab_bar.setContentsMargins(2, 2, 2, 0)
        tab_bar.setSpacing(8)
        self._sub_group = QButtonGroup(self)
        self._sub_group.setExclusive(True)
        self._sub_buttons: dict[str, tuple[QPushButton, str]] = {}

        setup_label = "✦ Game Setup" if needs_setup else "Launch && Setup"
        tab_defs = ([("setup", setup_label, "settings"), ("overview", "Overview", "info"), ("files", "Saves && Files", "folder")]
                    if needs_setup else
                    [("overview", "Overview", "info"), ("setup", setup_label, "settings"), ("files", "Saves && Files", "folder")])

        for key, text, ic_name in tab_defs:
            tb = QPushButton(f"  {text}")
            tb.setCheckable(True)
            tb.setCursor(Qt.PointingHandCursor)
            tb.setIconSize(QSize(15, 15))
            tb.clicked.connect(lambda _=False, k=key: self._switch_detail_tab(k))
            self._sub_group.addButton(tb)
            self._sub_buttons[key] = (tb, ic_name)
            tab_bar.addWidget(tb)
        tab_bar.addStretch(1)
        self.dl.addLayout(tab_bar)

        self._tab_pages: dict[str, QWidget] = {
            "overview": self._build_overview_tab(e, gen),
            "setup": self._build_setup_tab(e, needs_setup, gen),
            "files": self._build_files_tab(e, gen),
        }
        for key, _t, _ic in tab_defs:
            self.dl.addWidget(self._tab_pages[key])
        self.dl.addStretch(1)

        self._switch_detail_tab(chosen_tab, by_user=False)

        # Async data shared across tabs
        self._password = None
        self.bridge.call(self.bridge.core.entry_password(e.id),
                         lambda pw, s=e.source: gen == self._render_gen and self._show_password(s, pw), owner=self)
        self.bridge.call(self.bridge.core.library_archives(e.id),
                         lambda arcs: gen == self._render_gen and self._archives(e, arcs), owner=self)
        if e.install_path or e.download_path:
            self.bridge.call(self.bridge.core.library_call("candidates", e.id),
                             lambda cands: gen == self._render_gen and self._candidates(cands), owner=self)
        if self.env.get("linux") and e.executable and e.executable.lower().endswith((".exe", ".bat", ".cmd")):
            self.bridge.call(self.bridge.core.library_call("save_backups", e.id),
                             lambda r: gen == self._render_gen and self._show_saves(e, r), owner=self)
        self.bridge.call(self.bridge.core.new_game_check(e.id),
                         lambda st: gen == self._render_gen and self._show_check(e, st), lambda _err: None, owner=self)

    def _switch_detail_tab(self, key: str, *, by_user: bool = True) -> None:
        if key not in self._tab_pages:
            return
        if by_user:
            self._detail_tab = key
        pal = theme.CURRENT
        for pkey, pw in self._tab_pages.items():
            pw.setVisible(pkey == key)
        for k, (b, ic_name) in self._sub_buttons.items():
            active = k == key
            b.setChecked(active)
            b.setIcon(icon(ic_name, "#ffffff" if active else pal.muted))
            if active:
                b.setStyleSheet(
                    f"QPushButton {{ background: {pal.accent}; border: 1px solid {pal.accent}; color: #ffffff; "
                    f"padding: 6px 15px; border-radius: 10px; font-size: 9.4pt; font-weight: 700; }}"
                    f"QPushButton:hover {{ background: {pal.accent_hover}; }}"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: {pal.bg2}; border: 1px solid {pal.border}; color: {pal.text}; "
                    f"padding: 6px 15px; border-radius: 10px; font-size: 9.4pt; font-weight: 600; }}"
                    f"QPushButton:hover {{ border-color: {pal.accent}; background: {pal.bg3}; }}"
                )

    def _build_hero(self, e, running: bool, needs_setup: bool, gen: int) -> QFrame:
        hero = QFrame()
        hero.setObjectName("hero")
        hl = QHBoxLayout(hero)
        hl.setContentsMargins(20, 18, 20, 18)
        hl.setSpacing(22)

        self._hero_cover = _HeroCover(self.actions.covers, hero)
        self._sync_hero_cover_size()
        self._hero_cover.set_entry(e)
        hl.addWidget(self._hero_cover, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(8)

        # Top row: badges on left, quick icon actions on right
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(source_chip(e.source) if e.source else chip("Imported", theme.CURRENT.muted))
        if e.id in self.suspended:
            top.addWidget(chip("❚❚ Suspended", theme.CURRENT.warning))
        elif running:
            top.addWidget(chip("● " + self._status_text(e), theme.CURRENT.success))
        else:
            top.addWidget(chip(self._status_text(e), self._entry_status_color(e)))

        rel = e.release or {}
        if rel.get("version"):
            top.addWidget(chip(f"v{rel['version'].lstrip('vV')}", theme.CURRENT.muted))
        for col_name in e.collections[:2]:
            top.addWidget(chip(col_name, theme.CURRENT.accent))
        top.addStretch(1)

        fav_btn = tool("star", "Remove from favourites" if e.favorite else "Add to favourites",
                       on_click=lambda: self._organize(e, favorite=not e.favorite),
                       color="#f59e0b" if e.favorite else theme.CURRENT.muted)
        top.addWidget(fav_btn)

        folder_btn = tool("folder", "Open game folder",
                          on_click=lambda: open_folder(e.install_path or e.download_path or ""))
        folder_btn.setEnabled(bool(e.install_path or e.download_path))
        top.addWidget(folder_btn)

        props_btn = tool("settings", "Properties (executable, launch options, Proton)",
                         on_click=lambda: self._properties(e))
        top.addWidget(props_btn)

        more_btn = tool("more", "More options")
        more_btn.clicked.connect(lambda _=False, b=more_btn: self._more_menu(e, b))
        top.addWidget(more_btn)
        col.addLayout(top)

        # Title
        title_lbl = label(e.title, "title", wrap=True)
        title_lbl.setStyleSheet("font-size: 19pt; font-weight: 750;")
        col.addWidget(title_lbl)

        # Clean single-line stats + runner summary
        meta_bits = []
        if e.playtime_seconds >= 60:
            meta_bits.append(f"{playtime_text(e.playtime_seconds)} played")
        if e.last_played_at:
            meta_bits.append(f"Last played {relative_time(e.last_played_at)}")
        elif not needs_setup:
            meta_bits.append("Ready for first launch")
        else:
            meta_bits.append("Finish setup below to start playing")
        self._runs_with = label("   •   ".join(meta_bits), "small", wrap=True)
        self._hero_meta_prefix = "   •   ".join(meta_bits)
        col.addWidget(self._runs_with)

        if e.executable:
            self.bridge.call(self.bridge.core.library_call("preview", e.id),
                             lambda p: gen == self._render_gen and self._show_runs_with(p), owner=self)

        col.addStretch(1)

        # Action bar
        play_row = FlowLayout(spacing=8)
        play_row.addWidget(self._primary_button(e, running))
        if e.id in self.suspended:
            stop = button("  Stop", "close", on_click=lambda: self._stop(e))
            stop.setProperty("variant", "stop")
            stop.setMinimumHeight(44)
            play_row.addWidget(stop)

        installing = (e.setup or {}).get("state") in ("pending", "installing")
        self._fixes_btn = button("  Fixes", "wrench",
                                 tooltip="Compatibility tweaks & community fixes for this game",
                                 on_click=lambda: self._open_fixes(e))
        self._fixes_btn.setMinimumHeight(44)
        self._fixes_btn.setEnabled(bool(e.executable))
        if installing or not e.executable:
            self._fixes_btn.hide()
        play_row.addWidget(self._fixes_btn)

        diag = button("  Diagnose", "bug",
                      tooltip="Launch assistant: inspects logs and recommends fixes if the game fails to start",
                      on_click=lambda: self._diagnose(e))
        diag.setMinimumHeight(44)
        diag.setEnabled(bool(e.executable))
        if installing or not e.executable:
            diag.hide()
        play_row.addWidget(diag)

        if e.executable:
            self.bridge.call(self.bridge.core.library_call("fixes_for", e.id),
                             lambda items: gen == self._render_gen and self._fix_count(items), owner=self)

        col.addLayout(play_row)
        hl.addLayout(col, 1)
        return hero

    # ----------------------------------------------------------- Tab 1: Overview
    def _build_overview_tab(self, e, _gen: int) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(12)

        grid = QHBoxLayout()
        grid.setSpacing(12)

        # Left card: Launch & Compatibility glance
        lc, ll = card(margins=(16, 14, 16, 14), spacing=8)
        lhead = QHBoxLayout()
        lhead.addWidget(label("Launch & Compatibility", "h2"), 1)
        lhead.addWidget(button("Configure", "settings", "ghost", on_click=lambda: self._properties(e)))
        ll.addLayout(lhead)

        exe_name = Path(e.executable).name if e.executable else "Not selected yet"
        ll.addLayout(self._kv_row("Executable", exe_name))
        self._ov_runner_val = label("Checking…", "small", wrap=True)
        rrow = QHBoxLayout()
        rrow.addWidget(label("Environment", "muted"))
        rrow.addStretch(1)
        rrow.addWidget(self._ov_runner_val)
        ll.addLayout(rrow)
        if not e.executable:
            self._ov_runner_val.setText("Select an executable in Game Setup")

        from ezgd.library import updates
        rel = e.release or {}
        ver_str = (f"{rel['version']}  ·  {updates.describe(rel)}" if rel.get("version")
                   else ("Checked automatically" if e.game_id else "Manual import"))
        vrow = QHBoxLayout()
        vrow.addWidget(label("Version", "muted"))
        vrow.addStretch(1)
        vrow.addWidget(label(ver_str, "small"))
        if e.game_id:
            vrow.addWidget(tool("refresh", "Check for updates now", on_click=lambda: self._check_update(e),
                                color=theme.CURRENT.muted))
        ll.addLayout(vrow)

        self._check_holder = QVBoxLayout()
        ll.addLayout(self._check_holder)
        ll.addStretch(1)
        grid.addWidget(lc, 1)

        # Right card: Save Protection & Activity glance
        rc, rl = card(margins=(16, 14, 16, 14), spacing=8)
        rhead = QHBoxLayout()
        rhead.addWidget(label("Activity & Save Protection", "h2"), 1)
        rhead.addWidget(button("Saves && Files", "folder", "ghost",
                               on_click=lambda: self._switch_detail_tab("files")))
        rl.addLayout(rhead)
        rl.addLayout(self._kv_row("Play time", playtime_text(e.playtime_seconds)))
        rl.addLayout(self._kv_row("Sessions", f"{e.play_count} launch{'es' if e.play_count != 1 else ''}"))

        self._ov_saves_lbl = label("", "small", wrap=True)
        is_win_on_linux = bool(self.env.get("linux") and e.executable
                               and e.executable.lower().endswith((".exe", ".bat", ".cmd")))
        if is_win_on_linux:
            auto = self.actions.settings.get("auto_backup_saves", True)
            self._ov_saves_lbl.setText("Save auto-backup active — reading backups…" if auto
                                       else "Automatic save backups are disabled in Settings.")
        else:
            self._ov_saves_lbl.setText("Saves are stored by the game directly.")
        rl.addWidget(self._ov_saves_lbl)

        notes = _friendly_notes(e.notes)
        if notes:
            rl.addWidget(hline())
            rl.addWidget(label(notes, "small", wrap=True))
        rl.addStretch(1)
        grid.addWidget(rc, 1)

        lay.addLayout(grid)
        lay.addStretch(1)
        return page

    @staticmethod
    def _kv_row(key_text: str, val_text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(label(key_text, "muted"))
        row.addStretch(1)
        v = label(val_text)
        v.setStyleSheet("font-weight: 600;")
        row.addWidget(v)
        return row

    # ----------------------------------------------------------- Tab 2: Setup Studio & Launch
    def _build_setup_tab(self, e, needs_setup: bool, gen: int) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(12)

        xs = self.extraction_state.get(e.id)
        state = (e.setup or {}).get("state")

        if e.status is LibraryStatus.EXTRACTING and xs:
            lay.addWidget(self._extracting_card(xs))
        elif state in ("pending", "installing"):
            lay.addWidget(self._installer_card(e, gen))
        elif state == "waiting_steam":
            lay.addWidget(self._waiting_steam_card(e))
        elif needs_setup:
            lay.addWidget(self._setup_studio_card(e))

        # Clean Launch Executables selector card (always available here for switching DX11/DX12/multiplayer exes)
        lc, ll = card(margins=(16, 14, 16, 14), spacing=8)
        lhead = QHBoxLayout()
        lhead.addWidget(label("Launch Executable", "h2"), 1)
        lhead.addWidget(button("Browse…", "folder", "ghost", on_click=lambda: self._browse_executable(e)))
        lhead.addWidget(button("Launch options…", "settings", "ghost", on_click=lambda: self._properties(e)))
        ll.addLayout(lhead)
        ll.addWidget(label("Choose which executable or release launcher starts when you press Play.", "small"))

        self._launch_box = QVBoxLayout()
        self._launch_box.setSpacing(6)
        if e.install_path or e.download_path:
            self._launch_box.addWidget(label("Scanning game folder for executables…", "small"))
        else:
            self._launch_box.addWidget(label("Choose or extract a game folder first to scan for executables.", "small"))
        ll.addLayout(self._launch_box)
        self._launch_entry = e

        self._exe_combo = QComboBox(lc)
        self._exe_combo.hide()
        ll.addWidget(self._exe_combo)

        if needs_setup or state in ("pending", "installing"):
            lc.hide()
        lay.addWidget(lc)
        lay.addStretch(1)
        return page

    def _extracting_card(self, xs: dict) -> QFrame:
        c, cl = card(margins=(18, 16, 18, 16), spacing=10)
        head = QHBoxLayout()
        ic = label()
        ic.setPixmap(icon("folder", theme.CURRENT.info, 22).pixmap(22, 22))
        head.addWidget(ic)
        head.addWidget(label("Extracting Game Archive", "h2"), 1)
        head.addWidget(chip("In progress", theme.CURRENT.info))
        cl.addLayout(head)

        self._xlabel = ElidedLabel(f"Extracting {xs['archive']}…", "muted", c)
        self._xbar = QProgressBar()
        self._xbar.setRange(0, 1000)
        self._xbar.setFixedHeight(10)
        self._xbar.setValue(int(xs.get("progress", 0) * 1000))
        cl.addWidget(self._xlabel)
        cl.addWidget(self._xbar)
        if xs.get("id"):
            r = QHBoxLayout()
            r.addStretch(1)
            r.addWidget(button("Cancel extraction", "close", "danger",
                               on_click=lambda: self.bridge.call(self.bridge.core.cancel_extraction(xs["id"]))))
            cl.addLayout(r)
        return c

    def _waiting_steam_card(self, e) -> QFrame:
        base_name = (e.setup or {}).get("base") or "the required free base game"
        c, cl = card(margins=(18, 16, 18, 16), spacing=10)
        head = QHBoxLayout()
        head.addWidget(label("Steam Base Game Required", "h2"), 1)
        head.addWidget(chip("Step 1 of 2", theme.CURRENT.accent))
        cl.addLayout(head)
        cl.addWidget(label(f"Install “{base_name}” (free) in Steam first. Once Steam finishes downloading it, "
                           "EZ Games automatically applies the online fix and makes it ready to play.", "muted",
                           wrap=True))
        row = QHBoxLayout()
        row.addWidget(button(f"Install {base_name} in Steam", "download", "primary",
                             on_click=lambda: self._install_steam_base(e)))
        row.addStretch(1)
        cl.addLayout(row)
        return c

    def _setup_studio_card(self, e) -> QFrame:
        """Guided, visual 3-step Setup Studio card when a game needs setup."""
        pal = theme.CURRENT
        c, cl = card(margins=(18, 16, 18, 16), spacing=12)
        c.setStyleSheet(f"QFrame#card {{ background: {pal.bg2}; border: 1px solid {pal.accent}; border-radius: 14px; }}")

        has_files = bool(e.install_path)
        has_exe = bool(e.executable)
        needs_proton = bool(self.env.get("linux") and e.executable
                            and e.executable.lower().endswith((".exe", ".bat", ".cmd")))
        proton_ok = (not needs_proton) or bool(self.env.get("protons"))
        is_marked = e.status is LibraryStatus.INSTALLED and has_exe

        total_steps = 3
        done_steps = int(has_files) + int(has_exe) + int(is_marked and proton_ok)

        # Header + sleek step progress bar
        head = QHBoxLayout()
        ic = label()
        ic.setPixmap(icon("settings", pal.accent_hover, 22).pixmap(22, 22))
        head.addWidget(ic)
        htitle = label("Game Setup")
        htitle.setStyleSheet("font-size: 13.5pt; font-weight: 750;")
        head.addWidget(htitle)
        head.addWidget(label("Complete these quick steps to make the game ready to play", "muted"), 1)
        head.addWidget(chip(f"{done_steps} / {total_steps} complete",
                            pal.success if done_steps == total_steps else pal.accent))
        cl.addLayout(head)

        pbar = QProgressBar()
        pbar.setRange(0, 100)
        pbar.setValue(max(12, round(done_steps * 100 / total_steps)))
        pbar.setFixedHeight(6)
        cl.addWidget(pbar)

        # Step 1: Game Folder / Extraction
        s1_frame, s1 = self._step_tile(1, has_files, not has_files)
        s1_top = QHBoxLayout()
        s1_title = label("Game files ready" if has_files else "Locate or extract game files")
        s1_title.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        s1_top.addWidget(s1_title, 1)
        if has_files:
            s1_top.addWidget(button("Change folder…", "folder", "ghost", on_click=lambda: self._change_install(e)))
        else:
            s1_top.addWidget(button("Choose folder…", "folder", "primary", on_click=lambda: self._change_install(e)))
        s1.addLayout(s1_top)
        if has_files:
            s1.addWidget(PathLabel(e.install_path))
        else:
            s1.addWidget(label("Point to the extracted game folder, or unpack a downloaded archive below.",
                               "small", wrap=True))
            self._setup_archive_box = QHBoxLayout()
            self._setup_archive_box.setSpacing(8)
            s1.addLayout(self._setup_archive_box)
        cl.addWidget(s1_frame)

        # Step 2: Executable Selection
        s2_active = has_files and not has_exe
        s2_frame, s2 = self._step_tile(2, has_exe, s2_active)
        s2_top = QHBoxLayout()
        s2_title = label(f"Executable: {Path(e.executable).name}" if has_exe else "Choose the game executable")
        s2_title.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        s2_top.addWidget(s2_title, 1)
        if has_exe:
            s2_top.addWidget(button("Change…", "settings", "ghost", on_click=lambda: self._browse_executable(e)))
        else:
            s2_top.addWidget(button("Browse…", "folder", "ghost", on_click=lambda: self._browse_executable(e)))
        s2.addLayout(s2_top)
        if has_exe:
            s2.addWidget(PathLabel(e.executable))
        else:
            self._setup_quick_exes = QVBoxLayout()
            self._setup_quick_exes.setSpacing(6)
            self._setup_quick_exes.addWidget(
                label("Select a detected executable below, or click Browse… to pick one manually."
                      if (e.install_path or e.download_path)
                      else "Available once a game folder is chosen in Step 1.", "small", wrap=True)
            )
            s2.addLayout(self._setup_quick_exes)
        cl.addWidget(s2_frame)

        # Step 3: Compatibility & Finish
        s3_active = has_files and has_exe and not is_marked
        s3_frame, s3 = self._step_tile(3, is_marked and proton_ok, s3_active)
        s3_top = QHBoxLayout()
        if not proton_ok:
            s3_title = label("Install Proton / Wine runtime")
            s3_sub = "No Windows compatibility tool was found yet — install GE-Proton in one click."
        elif is_marked:
            s3_title = label("Ready to play")
            s3_sub = "All set — press Play anytime."
        else:
            s3_title = label("Confirm setup")
            s3_sub = ("Mark as Ready to Play to file it in your ready games (or press Play right away)."
                      if has_exe else "Complete Steps 1 and 2 first.")
        s3_title.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        s3_top.addWidget(s3_title, 1)
        if not proton_ok:
            s3_top.addWidget(button("Install Proton…", "download", "primary",
                                    on_click=lambda: self.actions.navigate.emit("system-check")))
        elif has_files and has_exe and not is_marked:
            s3_top.addWidget(button("Finish Setup", "check", "primary", on_click=lambda: self._mark_installed(e)))
        s3.addLayout(s3_top)
        s3.addWidget(label(s3_sub, "small", wrap=True))
        cl.addWidget(s3_frame)

        return c

    def _step_tile(self, num: int, done: bool, active: bool) -> tuple[QFrame, QVBoxLayout]:
        pal = theme.CURRENT
        f = QFrame()
        border = pal.success if done else (pal.accent if active else pal.border)
        bg = pal.bg1
        f.setStyleSheet(
            f"QFrame#stepTile {{ background: {bg}; border: 1px solid {border}; border-radius: 10px; }}"
        )
        f.setObjectName("stepTile")
        outer = QHBoxLayout(f)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(12)

        badge = QLabel("✓" if done else str(num))
        badge.setFixedSize(26, 26)
        badge.setAlignment(Qt.AlignCenter)
        b_col = pal.success if done else (pal.accent if active else pal.bg3)
        t_col = "#ffffff" if (done or active) else pal.muted
        badge.setStyleSheet(
            f"background: {b_col}; color: {t_col}; border-radius: 13px; font-weight: 800; font-size: 9.5pt; border: none;"
        )
        outer.addWidget(badge, 0, Qt.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(4)
        outer.addLayout(body, 1)
        return f, body

    def _browse_executable(self, e) -> None:
        start = e.install_path or e.download_path or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Select game executable for {e.title}",
            start,
            "Executables (*.exe *.bat *.cmd *.sh *.AppImage *.x86_64 *);;All files (*)",
        )
        if not path:
            return
        self._pick_executable_and_ready(e, path)

    def _pick_executable_and_ready(self, e, exe_path: str) -> None:
        install_dir = e.install_path or str(Path(exe_path).parent)
        self._call("mark_installed", e.id, install_dir, exe_path, ok_text="Game set up and ready to play")

    # ----------------------------------------------------------- Tab 3: Saves & Files
    def _build_files_tab(self, e, _gen: int) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(12)

        self._saves_holder = QVBoxLayout()
        lay.addLayout(self._saves_holder)

        c, cl = card(margins=(16, 14, 16, 14), spacing=10)
        head = QHBoxLayout()
        head.addWidget(label("Game Locations & Archives", "h2"), 1)
        head.addWidget(button("Change folder…", "folder", "ghost", on_click=lambda: self._change_install(e)))
        cl.addLayout(head)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(6)
        for name, path in (("Install folder", e.install_path), ("Download", e.download_path),
                           ("Executable", e.executable)):
            row = QHBoxLayout()
            row.addWidget(PathLabel(path or ""), 1)
            if path:
                row.addWidget(tool("folder", "Open in file manager",
                                   on_click=lambda p=path: open_folder(p), color=theme.CURRENT.muted))
            form.addRow(label(name, "muted"), row)
        cl.addLayout(form)

        self._password_holder = QVBoxLayout()
        cl.addLayout(self._password_holder)

        cl.addWidget(hline())
        ar_head = label("Extract an Archive", "muted")
        ar_head.setStyleSheet("font-weight: 650;")
        cl.addWidget(ar_head)
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
        self._archive_note = label("Checking for archives…", "small", wrap=True)
        cl.addWidget(self._archive_note)

        cl.addWidget(hline())
        urow = QHBoxLayout()
        urow.addWidget(label("Remove game files, prefix, or shortcut", "small"), 1)
        urow.addWidget(button("Remove entry…", "close", "ghost", on_click=lambda: self._remove(e)))
        urow.addWidget(button("Uninstall…", "trash", "danger",
                              tooltip="Delete game files, downloads and Proton prefix — you choose what goes",
                              on_click=lambda: self._uninstall(e)))
        cl.addLayout(urow)

        lay.addWidget(c)
        lay.addStretch(1)
        return page

    def select(self, entry_id: int) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == entry_id:
                self._last_select = 0.0
                self.list.setCurrentRow(i)
                if self.current_id != entry_id:
                    self._select_timer.stop()
                    self._render(entry_id)
                return

    # ---------------------------------------------------------- interrupted installs
    def _show_interrupted(self, e, info: dict | None) -> None:
        if not info:
            return
        b = Banner("warning", f"Installing {e.title} was interrupted ({info['name']}). Finish unpacking now.")
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
            self._alert_box.addWidget(b)
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- updates
    def _actionable_update_banner(self, e) -> QFrame:
        rel = e.release or {}
        st = rel.get("status")
        if rel.get("updating"):
            b = Banner("info", "Waiting for the new version download — once downloaded or imported, it updates over "
                               "your installed game while keeping saves and fixes.")
            b.add_action("Open game page", lambda: self._open_update_page(e), "external")
            b.add_action("Cancel", lambda: self.bridge.call(
                self.bridge.core.cancel_game_update(e.id), lambda _r: self.load(), owner=self), "close")
            return b
        if st == "update":
            b = Banner("warning", f"Update available: {rel.get('latest')} (installed: {rel.get('version')}). "
                                  "Saves, settings, and fixes are kept.")
            b.add_action(f"Update to {rel.get('latest')}", lambda: self._start_update(e), "download")
            return b
        b = Banner("info", f"Source page lists version {rel.get('latest')} (installed: {rel.get('version')}).")
        b.add_action("Compare & update", lambda: self._start_update(e), "external")
        return b

    def _check_update(self, e) -> None:
        self.actions.toasts.show("Checking for an update…", e.title)

        def done(res: list[dict]) -> None:
            r = next((x for x in res if x.get("entry_id") == e.id), None)
            if r is None:
                self.actions.toasts.show("No update information", "This game's page does not list a version.")
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
        backups = r.get("backups") or []
        try:
            if hasattr(self, "_ov_saves_lbl"):
                if backups:
                    latest_when = relative_time(backups[0]["created"])
                    self._ov_saves_lbl.setText(f"{len(backups)} save backup{'s' if len(backups) != 1 else ''}  ·  "
                                               f"Latest {latest_when}")
                elif r.get("has_prefix"):
                    self._ov_saves_lbl.setText(f"Active prefix: {r.get('current_files', 0)} save file(s) "
                                               f"({format_bytes(r.get('current_bytes') or 0)})")
                else:
                    self._ov_saves_lbl.setText("Save prefix is created automatically on first launch.")
        except RuntimeError:
            pass

        try:
            clear_now(self._saves_holder)
        except RuntimeError:
            return

        c, cl = card(margins=(16, 14, 16, 14), spacing=8)
        auto = self.actions.settings.get("auto_backup_saves", True)
        tip = (("Backed up automatically after every session, and before updates, uninstalls and prefix rebuilds."
                if auto else "Automatic backups are off (Settings → Library).")
               + " Backups are kept per game title so a reinstall finds them again.")
        c.setToolTip(tip)

        head = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icon("save", theme.CURRENT.accent, 20).pixmap(20, 20))
        head.addWidget(ic)
        head.addWidget(label("Save Backups", "h2"), 1)
        b_now = button("Back up now", "save", on_click=lambda: self._backup_now(e))
        b_now.setEnabled(bool(r.get("has_prefix")))
        head.addWidget(b_now)
        head.addWidget(tool("folder", "Open save backups folder", on_click=lambda: open_folder(r["folder"]),
                            color=theme.CURRENT.muted))
        cl.addLayout(head)

        if r.get("has_prefix"):
            where = ", ".join(r.get("current_folders") or []) or "no save files detected yet"
            cl.addWidget(label(f"In prefix now: {r.get('current_files', 0)} file(s), "
                               f"{format_bytes(r.get('current_bytes') or 0)} — {where}", "small", wrap=True))
        else:
            cl.addWidget(label("Proton prefix is created on first launch; saves are backed up automatically after play.",
                               "small", wrap=True))

        if not backups:
            cl.addWidget(label("No restore points saved yet.", "muted"))
        for b in backups[:6]:
            row = QHBoxLayout()
            when = relative_time(b["created"])
            t = label(f"{b['reason_text']}  ·  {when}")
            t.setStyleSheet("font-weight: 600;")
            t.setToolTip(b["path"] + ("\n" + "\n".join(b.get("folders") or []) if b.get("folders") else ""))
            row.addWidget(t)
            row.addWidget(label(f"{b['files']} file(s) · {format_bytes(b['bytes'])}", "small"), 1)
            row.addWidget(button("Restore…", "retry", "ghost", on_click=lambda b=b: self._restore_saves(e, b)))
            row.addWidget(tool("trash", "Delete this backup", on_click=lambda b=b: self._delete_backup(e, b),
                               color=theme.CURRENT.muted))
            cl.addLayout(row)
        if len(backups) > 6:
            cl.addWidget(label(f"{len(backups) - 6} older backup(s) in folder.", "small"))
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
            b = button("  Resume", "play", tooltip="Continues right where it was frozen",
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
            b = button("  Installing…", "refresh", tooltip="The installer is running in its own window")
            b.setProperty("variant", "play")
            b.setEnabled(False)
        elif state == "waiting_steam":
            b = button("  Install in Steam", "download",
                       tooltip=f"Install “{e.setup.get('base')}” in Steam (free); the app then adds the fix automatically",
                       on_click=lambda: self._install_steam_base(e))
            b.setProperty("variant", "play")
            b.setIcon(icon("download", "#ffffff"))
        elif state == "pending":
            b = button("  Install game", "download", tooltip="Run this repack's setup.exe installer",
                       on_click=lambda: self._install(e))
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
        for f in items:
            b = Banner("warning", f"Before you play: {f['title']}. " + f["explanation"].split(". ")[0] + ".")
            btn = b.add_action(f["fix"]["label"], None, "wrench")

            def run(_=False, f=f, btn=btn) -> None:
                btn.setEnabled(False)
                btn.setText("Installing…" if f["fix"]["kind"] == "winetricks" else "Working…")

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
                self._alert_box.addWidget(b)
            except RuntimeError:
                return

    def _last_run_failed(self, e, st: dict) -> None:
        if not st.get("failed"):
            return
        text = f"Last launch exited after {st['seconds']}s (code {st['exit_code']})"
        text += f" — {st['meaning'].split(':')[0]}." if st.get("meaning") else "."
        b = Banner("warning", text + " Open Diagnose to find the cause and apply a fix.")
        b.add_action("Diagnose", lambda: self._diagnose(e), "bug")
        try:
            self._alert_box.addWidget(b)
        except RuntimeError:
            pass

    def _install_steam_base(self, e) -> None:
        from ezgd.ui.dialogs.misc import open_url

        def done(r: dict) -> None:
            if r.get("opened") == "store" and r.get("url"):
                open_url(r["url"])
            self.actions.toasts.show("Install it in Steam", f"Press “Install” for {r['name']} in Steam. When Steam "
                                     "has finished, the app adds the fix automatically.")
        if e.game_id:
            self.bridge.call(self.bridge.core.install_steam_base(e.game_id), done,
                             lambda exc: self.actions.fail("Steam", exc), owner=self)

    def eventFilter(self, obj, ev) -> bool:                      # noqa: N802
        if ev.type() == QEvent.Resize and obj is self.filter:
            self._place_filter_icon()
        if ev.type() == QEvent.Resize and obj is self.detail_scroll.viewport():
            sp = self.detail_scroll.parentWidget()
            if hasattr(sp, "sizes") and self.width() < 900 and sp.sizes()[0] > 240:
                sp.setSizes([240, max(300, sum(sp.sizes()) - 240)])
            self._sync_hero_cover_size()
        return super().eventFilter(obj, ev)

    def _cover_size(self) -> tuple[int, int]:
        w = self.detail_scroll.viewport().width() if hasattr(self, "detail_scroll") else 900
        return (272, 153) if w >= 780 else (208, 117) if w >= 580 else (152, 86) if w >= 460 else (0, 0)

    def _sync_hero_cover_size(self) -> None:
        cover = getattr(self, "_hero_cover", None)
        if cover is None:
            return
        try:
            cw, ch = self._cover_size()
            cover.setVisible(cw > 0)
            if cw and cover.width() != cw:
                cover.setFixedSize(cw, ch)
        except RuntimeError:
            pass

    def _show_runs_with(self, p: dict) -> None:
        try:
            if p["ok"]:
                short = p["description"].split(" — ")[0]
                prefix = getattr(self, "_hero_meta_prefix", "")
                self._runs_with.setText((f"{prefix}   •   {short}") if prefix else short)
            else:
                self._runs_with.setText("⚠ " + p["error"])
            if hasattr(self, "_ov_runner_val"):
                self._ov_runner_val.setText(p["description"].split(" — ")[0] if p["ok"] else ("⚠ " + p["error"]))
        except RuntimeError:
            pass

    # ------------------------------------------------------------ installers
    def _installer_card(self, e, gen: int) -> QFrame:
        pal = theme.CURRENT
        c, cl = card(margins=(18, 16, 18, 16), spacing=12)
        c.setStyleSheet(f"QFrame#card {{ background: {pal.bg2}; border: 1px solid {pal.accent}; border-radius: 14px; }}")
        installing = (e.setup or {}).get("state") == "installing"

        # Clean 3-stage visual stepper header
        steps_row = QHBoxLayout()
        steps_row.setSpacing(8)
        steps_row.addWidget(chip("✓ 1. Downloaded", pal.success))
        steps_row.addWidget(label("──▸", "small"))
        steps_row.addWidget(chip("● 2. Running Installer…" if installing else "● 2. Run Repack Installer",
                                 pal.info if installing else pal.accent))
        steps_row.addWidget(label("──▸", "small"))
        steps_row.addWidget(chip("3. Ready to Play", pal.muted))
        steps_row.addStretch(1)
        cl.addLayout(steps_row)

        htitle = label("Installing in external window…" if installing else "Run Repack Installer")
        htitle.setStyleSheet("font-size: 13.5pt; font-weight: 750;")
        cl.addWidget(htitle)

        proton = " via Proton" if self.env.get("linux") else ""
        desc = ("Finish the setup wizard in its own window — when it closes, EZ Games automatically detects the "
                "installed game and configures Play."
                if installing else
                f"This download is a compressed repack installer. Click Install Game to launch setup.exe{proton}, "
                "choose your components, and EZ Games will link the installed executable automatically.")
        cl.addWidget(label(desc, "muted", wrap=True))

        if (e.setup or {}).get("last_result") and not installing:
            cl.addWidget(Banner("warning", e.setup["last_result"]))

        info_tile = QFrame()
        info_tile.setObjectName("repackInfo")
        info_tile.setStyleSheet(
            f"QFrame#repackInfo {{ background: {pal.bg1}; border: 1px solid {pal.border}; border-radius: 10px; }}"
        )
        self._inst_box = QVBoxLayout(info_tile)
        self._inst_box.setContentsMargins(12, 10, 12, 10)
        self._inst_box.setSpacing(4)
        self._inst_box.addWidget(label("Reading repack details…", "small"))
        cl.addWidget(info_tile)

        if not installing:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(button("Install game…", "download", "primary", on_click=lambda: self._install(e)))
            self._verify_btn = button("Verify BIN files (MD5)", "check", "ghost",
                                      tooltip="Compare repack data files against the included MD5 checksums",
                                      on_click=lambda: self._verify(e))
            self._verify_btn.setEnabled(False)
            row.addWidget(self._verify_btn)
            row.addStretch(1)
            cl.addLayout(row)

        self.bridge.call(self.bridge.core.library_call("installer_info", e.id),
                         lambda info: gen == self._render_gen and self._installer_details(info), owner=self)
        return c

    def _installer_details(self, info: dict | None) -> None:
        if not info:
            return
        try:
            clear_now(self._inst_box)
        except RuntimeError:
            return
        data = f"{info.get('data_files', 0)} data file(s), {format_bytes(info.get('data_bytes') or 0)}"
        rows = [("Installer", f"{Path(info.get('setup') or '').name}  ·  {info.get('label', 'installer')} ({data})")]
        if info.get("target"):
            rows.append(("Target folder", f"{info['target']}"))
        if info.get("optional"):
            rows.append(("Optional packs", ", ".join(info["optional"][:6])))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(4)
        for k, v in rows:
            form.addRow(label(k, "muted"), label(v, "small", wrap=True, selectable=True))
        self._inst_box.addLayout(form)
        if hasattr(self, "_verify_btn"):
            try:
                self._verify_btn.setEnabled(bool(info.get("md5")))
                if not info.get("md5"):
                    self._verify_btn.setToolTip("This repack does not include an MD5 checksum list.")
            except RuntimeError:
                pass

    def _repack_left(self, e, info: dict | None) -> None:
        if not info:
            return
        if info.get("state") == "pending" and not (e.setup or {}).get("state"):
            self.load()
            return
        if info.get("state") != "installed" or not info.get("repack_left"):
            return
        b = Banner("info", f"Repack installer files ({format_bytes(info.get('repack_bytes') or 0)}) are still in "
                           "Downloads and no longer needed to play.")
        b.add_action("Free up space…", lambda: self._delete_repack(e, info), "trash")
        try:
            self._alert_box.addWidget(b)
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
                                "it up for Play.", None, wrap=True))
            verify = QCheckBox("Verify repack files first (MD5) — recommended after downloading")
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
        self.actions.toasts.show("Checking repack files…", "Reading data files — big repacks take a few minutes.", "info")
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
                   "Installing would most likely fail part-way. For a torrent, use “Check files” on the download; "
                   "for file-host downloads, download those parts again.")
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
        def ok(_r: dict) -> None:
            self.actions.toasts.show("Installer started", "Finish it in its window — the app sets up the game when "
                                                          "it closes.", "success")
            self.load()
        self.bridge.call(self.bridge.core.library_call("run_installer", e.id, True), ok,
                         lambda err: self.actions.fail("Could not start the installer", err))

    def _delete_repack(self, e, info: dict) -> None:
        r = QMessageBox.question(self, "Delete repack files",
                                 f"Delete the repack files of “{e.title}” ({format_bytes(info.get('repack_bytes') or 0)})?"
                                 f"\n\n{info.get('root')}\n\nThe installed game is kept.")
        if r != QMessageBox.Yes:
            return
        self.bridge.call(self.bridge.core.library_call("delete_repack", e.id),
                         lambda freed: (self.actions.toasts.show("Repack files deleted", f"Freed {format_bytes(freed)}.",
                                                                 "success"), self.load()),
                         lambda err: self.actions.fail("Delete repack files", err))

    # ----------------------------------------------------------- fixes & diagnostics
    def _show_suggestions(self, e, items: list[dict]) -> None:
        actionable = [s for s in (items or []) if s.get("changes")]
        if not actionable:
            return
        for s in actionable[:3]:
            b = Banner(s["severity"], f"Recommended fix — {s['title']}: {s['detail']}")
            b.add_action("Apply fix", lambda k=s["key"], t=s["title"]: self._apply_suggestion(e, k, t), "check")
            b.add_action("All fixes…", lambda: self._open_fixes(e), "wrench")
            try:
                self._alert_box.addWidget(b)
            except RuntimeError:
                return

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
            clear_now(self._password_holder)
            if e is not None and e.status is not LibraryStatus.INSTALLED:
                # Needs extraction/setup: surface password banner prominently in alerts too
                self._alert_box.addWidget(PasswordBanner(source, password))
            row = QHBoxLayout()
            row.addWidget(label(f"Archive password: {password}", "small", selectable=True))
            row.addWidget(button("Copy", "link", "ghost",
                                 on_click=lambda: QGuiApplication.clipboard().setText(password)))
            row.addStretch(1)
            self._password_holder.addLayout(row)
        except RuntimeError:
            pass

    def _dupes(self, dupes: list) -> None:
        if not dupes:
            return
        names = ", ".join(f"{d.title} ({STATUS_TEXT.get(str(d.status), d.status)})" for d in dupes[:3])
        try:
            self._alert_box.addWidget(Banner("warning", f"Possible duplicate entry in Library: {names}."))
        except RuntimeError:
            pass

    def _archives(self, e, archives: list[str]) -> None:
        try:
            self._archive_combo.clear()
        except RuntimeError:
            return
        for a in archives:
            self._archive_combo.addItem(Path(a).name, a)
        self._extract_btn.setEnabled(bool(archives))
        self._archive_note.setText(
            "Archives unpack safely to a staging directory before moving into place."
            if archives else "No archives detected in this game's folders."
        )
        if archives and hasattr(self, "_setup_archive_box"):
            try:
                clear_now(self._setup_archive_box)
                first_name = Path(archives[0]).name
                self._setup_archive_box.addWidget(
                    button(f"Extract {first_name}…", "folder", "ghost", on_click=lambda: self._extract(e))
                )
                self._setup_archive_box.addStretch(1)
            except RuntimeError:
                pass

    def _candidates(self, cands: list) -> None:
        try:
            self._exe_combo.clear()
        except RuntimeError:
            return
        for c in cands[:25]:
            self._exe_combo.addItem(c.label, str(c.path))
            self._exe_combo.setItemData(self._exe_combo.count() - 1, c.details, Qt.ToolTipRole)
        self._show_launch_files(cands)
        self._populate_setup_quick_exes(cands)

    def _populate_setup_quick_exes(self, cands: list) -> None:
        box = getattr(self, "_setup_quick_exes", None)
        e = getattr(self, "_launch_entry", None)
        if box is None or e is None or e.executable:
            return
        try:
            clear_now(box)
        except RuntimeError:
            return
        games = [c for c in cands if c.kind not in ("tool", "installer")]
        shown = (games or cands)[:4]
        if not shown:
            box.addWidget(label("No executables detected automatically — click Browse… to pick one.", "small"))
            return
        pal = theme.CURRENT
        for idx, c in enumerate(shown):
            tile = QFrame()
            tile.setStyleSheet(
                f"QFrame {{ background: {pal.bg2}; border: 1px solid {pal.border}; border-radius: 8px; }}"
                "QFrame QLabel { border: none; background: transparent; }"
            )
            row = QHBoxLayout(tile)
            row.setContentsMargins(10, 7, 10, 7)
            row.setSpacing(8)
            name = label(c.path.name)
            name.setStyleSheet("font-weight: 700;")
            name.setToolTip(c.details)
            row.addWidget(name)
            if idx == 0 and c.kind not in ("tool", "installer"):
                row.addWidget(chip("Recommended", pal.success))
            for m in c.modes[:2]:
                row.addWidget(chip(m, pal.accent))
            row.addStretch(1)
            row.addWidget(button("Select && Finish", "check", "primary" if idx == 0 else None,
                                 tooltip=c.details,
                                 on_click=lambda p=str(c.path): self._pick_executable_and_ready(e, p)))
            box.addWidget(tile)

    def _show_launch_files(self, cands: list) -> None:
        try:
            clear_now(self._launch_box)
        except RuntimeError:
            return
        e = self._launch_entry
        if not cands:
            self._launch_box.addWidget(label("No executables found in this folder. Click Browse… above to select one.",
                                             "small", wrap=True))
            return
        pal = theme.CURRENT
        games = [c for c in cands if c.kind not in ("tool", "installer")]
        shown = (games or cands)[:7]
        if e.executable and not any(str(c.path) == e.executable for c in shown):
            shown += [c for c in cands if str(c.path) == e.executable]
        for c in shown:
            active = str(c.path) == e.executable
            tile = QFrame()
            border = pal.success if active else pal.border
            tile.setStyleSheet(
                f"QFrame#exeTile {{ background: {pal.bg1}; border: 1px solid {border}; border-radius: 9px; }}"
            )
            tile.setObjectName("exeTile")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(12, 8, 12, 8)
            tl.setSpacing(3)

            row = QHBoxLayout()
            row.setSpacing(6)
            name = label(c.path.name)
            name.setStyleSheet("font-weight: 650;")
            name.setToolTip(c.details)
            row.addWidget(name)
            for m in c.modes:
                row.addWidget(chip(m, pal.accent))
            if c.kind == "launch script":
                row.addWidget(chip(f"Launcher → {c.starts}", pal.success))
            elif c.kind == "tool":
                row.addWidget(chip("Helper / Tool", pal.warning))
            elif c.kind == "installer":
                row.addWidget(chip("Installer", pal.accent))
            row.addStretch(1)
            if active:
                row.addWidget(chip("✓ Active for Play", pal.success))
            else:
                row.addWidget(button("Use for Play", variant="ghost", tooltip=c.details,
                                     on_click=lambda p=str(c.path): self._pick_executable_and_ready(e, p)))
            tl.addLayout(row)

            reason = c.reason + (f" · prepares: {' · '.join(c.prepares[:3])}" if c.prepares else "")
            if reason:
                tl.addWidget(label(reason, "small", wrap=True))
            self._launch_box.addWidget(tile)

        hidden = len(cands) - len(shown)
        if hidden > 0:
            self._launch_box.addWidget(label(f"+ {hidden} helper/installer file(s) available in Properties.", "small"))

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

    def _show_check(self, _e, st: dict) -> None:
        status = st.get("status")
        if status == "problem":
            b = Banner("warning", "Pre-launch check found an issue: " + (st.get("detail") or "")
                                  + " (see Diagnose for details).")
            if st.get("report"):
                from ezgd.ui.dialogs.diagnose import _open_file
                rep = Path(st["report"])
                b.actions.addWidget(button("Report", "folder", on_click=lambda: _open_file(rep)))
            try:
                self._alert_box.addWidget(b)
            except RuntimeError:
                pass
            return
        labels = {
            "waiting": ("Pre-launch check queued when idle", theme.CURRENT.muted),
            "checking": ("Running pre-launch check…", theme.CURRENT.info),
            "ready": ("✓ Verified ready before first launch", theme.CURRENT.success),
            "fixed": ("✓ Auto-fixed before first launch", theme.CURRENT.success),
        }
        if status not in labels:
            return
        txt, col = labels[status]
        try:
            clear_now(self._check_holder)
            self._check_holder.addWidget(chip(txt, col))
        except RuntimeError:
            pass

    def _froze(self, e) -> None:
        self.bridge.call(self.bridge.core.report_freeze(e.id),
                         lambda verdict: self.actions.toasts.show(f"{e.title} stopped", f"Kept what it was doing: "
                                                                   f"{verdict} Diagnose → Fix my game starts from it.",
                                                                   "success"),
                         lambda err: self.actions.fail("Could not stop the game", err), owner=self)

    def _resume(self, e) -> None:
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
        if e.game_id:
            m.addAction("Check for updates", lambda: self._check_update(e))
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
            form.addRow("", label(f"Auto-filled with the password published by {theme.source_name(e.source)}.",
                                  "small", wrap=True))
        lay.addWidget(label(Path(archive).name, "h2"))
        lay.addLayout(form)
        lay.addWidget(label("Files unpack to a staging folder first and move into place once validated.",
                            "small", wrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        self.bridge.call(self.bridge.core.extract_entry(e.id, archive, dest.text().strip() or None,
                                                        pw.text() or None),
                         lambda _ok: self.load(), lambda err: self.actions.fail("Extraction", err))
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
        from ezgd.ui.dialogs.uninstall import open_uninstall
        r = open_uninstall(self.actions, self.entries.get(e.id, e), self)
        if r is None:
            self.load()
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
            path, _ = QFileDialog.getOpenFileName(
                self, "Choose the archive", self._import_start(),
                "Archives (*.zip *.7z *.rar *.tar *.gz *.xz *.bz2 *.001);;All files (*)",
            )
        else:
            return
        if path:
            self.import_path(path)

    def _import_start(self) -> str:
        for p in (Path.home() / "Downloads", Path(self.actions.settings.get("download_dir") or "")):
            if str(p) and p.is_dir():
                return str(p)
        return str(Path.home())

    def import_path(self, path: str) -> None:
        from ezgd.ui.dropin import guess_title
        title, ok = QInputDialog.getText(self, "Import game", f"Title for “{Path(path).name}”:",
                                         text=guess_title(path))
        if not (ok and title.strip()):
            return

        def imported(entry):
            self.actions.toasts.show("Imported into the library", entry.title, "success")
            self.current_id = entry.id
            self.load()
            self.bridge.call(self.bridge.core.lookup_steam_app_id(entry.id), lambda _r: self.load(),
                             lambda _e: None, owner=self)
        self.bridge.call(self.bridge.core.library_call("import_folder", title.strip(), path), imported,
                         lambda err: self.actions.fail("Import", err))
