"""Downloads: live queue with per-item actions, filters, details and torrent file selection."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ezgd.core.fsutil import format_bytes, format_eta, format_speed
from ezgd.ui import theme
from ezgd.ui.dialogs.misc import open_folder, open_url
from ezgd.ui.icons import icon
from ezgd.ui.widgets.common import EmptyState, FlowLayout, button, chip, label, set_chip, source_chip, spin_box, tool

STATUS_TEXT = {"queued": "Queued", "metadata": "Fetching metadata", "checking": "Checking", "downloading":
               "Downloading", "paused": "Paused", "verifying": "Verifying", "completed": "Download complete",
               "failed": "Failed", "cancelled": "Cancelled"}
FILTERS = [("all", "All"), ("active", "Active"), ("queued", "Queued"), ("paused", "Paused"),
           ("completed", "Completed"), ("failed", "Failed")]
ACTIVE = {"metadata", "checking", "downloading", "verifying"}
VERIFICATION_TEXT = {"pieces": "Every piece verified by the torrent engine",
                     "verified": "SHA-256 checksum verified against the host's published value",
                     "mismatch": "Checksum mismatch — the file was corrupt and deleted",
                     "none": "Downloaded; no trusted checksum was available, so it is not verified"}


def matches_filter(snap: dict, key: str) -> bool:
    s = snap["status"]
    return {"all": True, "active": s in ACTIVE, "queued": s == "queued", "paused": s == "paused",
            "completed": s == "completed", "failed": s in ("failed", "cancelled")}[key]


class DownloadRow(QFrame):
    def __init__(self, page: "DownloadsPage", snap: dict):
        super().__init__()
        self.page = page
        self.snap = snap
        self.extraction: dict | None = None
        self.setProperty("card", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(12)
        self.icon = QLabel()
        lay.addWidget(self.icon, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = label("")
        self.title.setStyleSheet("font-weight: 650; font-size: 10.5pt;")
        top.addWidget(self.title, 1)
        if snap.get("source"):
            top.addWidget(source_chip(snap["source"]))
        if snap.get("archive_password"):
            lock = chip(f"🔒 password: {snap['archive_password']}", theme.CURRENT.warning)
            lock.setToolTip("The archives from this download are password-protected. The app uses this password "
                            "automatically when extracting; use “Copy archive password” in the ⋯ menu to unpack "
                            "them yourself.")
            lock.setTextInteractionFlags(Qt.TextSelectableByMouse)
            top.addWidget(lock)
        self.status = chip("", theme.CURRENT.muted)
        top.addWidget(self.status)
        col.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(8)
        col.addWidget(self.bar)
        self.info = label("", "small")
        col.addWidget(self.info)
        self.extra = label("", "small", wrap=True)
        col.addWidget(self.extra)
        lay.addLayout(col, 1)
        self.btn_pause = tool("pause", "Pause", lambda: page.command("pause", self.snap["id"]))
        self.btn_resume = tool("play", "Resume", lambda: page.command("resume", self.snap["id"]),
                               theme.CURRENT.success)
        self.btn_retry = tool("retry", "Retry", lambda: page.command("retry", self.snap["id"]))
        self.btn_cancel = tool("close", "Cancel", lambda: page.cancel(self.snap))
        self.btn_folder = tool("folder", "Open folder", lambda: open_folder(self.snap["dest_dir"]))
        self.btn_more = tool("more", "More actions", self._menu)
        for b in (self.btn_pause, self.btn_resume, self.btn_retry, self.btn_cancel, self.btn_folder, self.btn_more):
            lay.addWidget(b, 0, Qt.AlignVCenter)
        self.update_snap(snap)

    def update_snap(self, snap: dict) -> None:
        self.snap = snap
        s = snap["status"]
        self.icon.setPixmap(icon("magnet" if snap["kind"] == "torrent" else "download",
                                 theme.status_color(s), 22).pixmap(22, 22))
        self.title.setText(snap["title"])
        set_chip(self.status, STATUS_TEXT.get(s, s), theme.status_color(s))
        self.bar.setValue(int(snap["progress"] * 1000))
        state = {"completed": "done", "failed": "error", "cancelled": "error", "paused": "paused"}.get(s, "")
        if self.bar.property("state") != state:
            self.bar.setProperty("state", state)
            self.bar.style().unpolish(self.bar)
            self.bar.style().polish(self.bar)
        done, total = snap["downloaded_bytes"], snap["total_bytes"]
        parts = [f"{format_bytes(done)} of {format_bytes(total)}" if total else format_bytes(done)]
        if s in ACTIVE:
            parts.append(format_speed(snap["speed"]))
            parts.append(f"ETA {format_eta(snap['eta'])}")
        if snap["kind"] == "torrent" and (s in ACTIVE or snap["status_detail"] == "Seeding"):
            parts.append(f"{snap['peers']} peers ({snap['seeds']} seeds)")
            if snap["upload_speed"]:
                parts.append(f"↑ {format_speed(snap['upload_speed'])}")
        if snap["priority"] > 0:
            parts.append("high priority")
        self.info.setText(" · ".join(parts))
        detail = snap["error"] if s == "failed" else snap["status_detail"]
        lines = [detail] if detail else []
        if self.extraction:
            x = self.extraction
            if x["status"] == "running":
                lines.append(f"Extracting {x['archive']}: {x['progress'] * 100:.0f}%")
            elif x["status"] == "completed":
                lines.append("Extracted — finish the installation in Library (choose the executable).")
            elif x["status"] == "failed":
                lines.append(f"Extraction failed: {x.get('error', '')}")
        elif s == "completed":
            lines.append("Transfer finished. Extraction and installation are tracked separately in Library.")
        self.extra.setText("\n".join(lines))
        self.extra.setStyleSheet(f"color: {theme.CURRENT.danger};" if s == "failed" else "")
        self.extra.setVisible(bool(lines))
        finished = s in ("completed", "failed", "cancelled")
        self.btn_pause.setVisible(not finished and s != "paused")
        self.btn_resume.setVisible(s == "paused")
        self.btn_retry.setVisible(s in ("failed", "cancelled"))
        self.btn_cancel.setVisible(not finished)

    def set_extraction(self, state: dict) -> None:
        self.extraction = state
        self.update_snap(self.snap)

    def _menu(self) -> None:
        m = QMenu(self)
        sid = self.snap["id"]
        if self.snap["priority"] <= 0:
            m.addAction("Download first (high priority)", lambda: self.page.command("set_priority", sid, 5))
        else:
            m.addAction("Normal priority", lambda: self.page.command("set_priority", sid, 0))
        if self.snap["kind"] == "torrent":
            m.addAction("Verify files (re-check pieces)", lambda: self.page.command("recheck", sid))
        if self.snap["downloaded_bytes"] == 0 and self.snap["status"] not in ACTIVE:
            m.addAction("Change destination…", lambda: self.page.change_destination(self.snap))
        if self.snap.get("source_url"):
            m.addAction("Open game page", lambda: open_url(self.snap["source_url"]))
        if self.snap.get("archive_password"):
            m.addAction("Copy archive password",
                        lambda: QGuiApplication.clipboard().setText(self.snap["archive_password"]))
        if self.snap.get("error"):
            m.addAction("Copy error message", lambda: QGuiApplication.clipboard().setText(self.snap["error"]))
        m.addSeparator()
        m.addAction("Remove from list", lambda: self.page.remove(self.snap, False))
        m.addAction("Remove and delete downloaded files…", lambda: self.page.remove(self.snap, True))
        m.exec(self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft()))


class BrowserRow(QFrame):
    """One transfer done by the in-app browser (after the user's check and click)."""

    def __init__(self, page: "DownloadsPage", snap: dict):
        super().__init__()
        self.page = page
        self.did = snap["id"]
        self.setProperty("card", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 10, 8)
        col = QVBoxLayout()
        col.setSpacing(3)
        top = QHBoxLayout()
        self.name = label(snap["name"])
        self.name.setStyleSheet("font-weight: 650;")
        top.addWidget(self.name, 1)
        top.addWidget(chip(f"via {snap['host']} · in-app browser", theme.CURRENT.muted))
        col.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        col.addWidget(self.bar)
        self.info = label("", "small")
        col.addWidget(self.info)
        lay.addLayout(col, 1)
        self.pause_btn = button("", "pause", "ghost", tooltip="Pause", on_click=self._toggle)
        self.cancel_btn = button("", "close", "ghost", tooltip="Cancel", on_click=self._cancel)
        self.folder_btn = button("", "folder", "ghost", tooltip="Show file",
                                 on_click=lambda: open_folder(self.snap["path"]))
        self.remove_btn = button("", "trash", "ghost", tooltip="Remove from this list (the file stays)",
                                 on_click=lambda: self.page.browser().remove(self.did))
        for b in (self.pause_btn, self.cancel_btn, self.folder_btn, self.remove_btn):
            lay.addWidget(b)
        self.update_snap(snap)

    def update_snap(self, s: dict) -> None:
        self.snap = s
        total, got = s["total"], s["received"]
        self.bar.setValue(int(got * 1000 / total) if total else (1000 if s["state"] == "completed" else 0))
        parts = [f"{s['game']}" if s["game"] else ""]
        if s["state"] in ("downloading", "paused"):
            parts.append(f"{format_bytes(got)} / {format_bytes(total)}" if total else format_bytes(got))
            if s["state"] == "downloading" and s["speed"]:
                parts.append(format_speed(s["speed"]))
                if s["eta"] is not None:
                    parts.append(f"{format_eta(s['eta'])} left")
            if s["state"] == "paused":
                parts.append("paused")
        elif s["state"] == "completed":
            lib = s["library"]
            parts.append({"adding": "done · adding to your Library…", "added": "done · added to your Library",
                          "": "done"}.get(lib, f"done · {lib}"))
        else:
            parts.append("cancelled" if s["state"] == "cancelled" else f"failed: {s['error']}")
        self.info.setText(" · ".join(p for p in parts if p))
        running = s["state"] in ("downloading", "paused")
        self.pause_btn.setVisible(running)
        self.cancel_btn.setVisible(running)
        self.remove_btn.setVisible(not running)
        self.folder_btn.setVisible(s["state"] == "completed")
        paused = s["state"] == "paused"
        self.pause_btn.setIcon(icon("play" if paused else "pause", theme.CURRENT.text))
        self.pause_btn.setToolTip("Resume" if paused else "Pause")

    def _toggle(self) -> None:
        m = self.page.browser()
        (m.resume if self.snap["state"] == "paused" else m.pause)(self.did)

    def _cancel(self) -> None:
        self.page.browser().cancel(self.did)


class DownloadsPage(QWidget):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.rows: dict[int, tuple[QListWidgetItem, DownloadRow]] = {}
        self.extractions: dict[int, dict] = {}
        self.filter_key = "all"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 16)
        lay.setSpacing(12)
        head = QHBoxLayout()
        col = QVBoxLayout()
        col.addWidget(label("Downloads", "title"))
        self.summary = label("", "muted")
        col.addWidget(self.summary)
        head.addLayout(col, 1)
        head.addWidget(button("Pause all", "pause", on_click=lambda: self.bridge.call(self.bridge.core.pause_all())))
        head.addWidget(button("Resume all", "play", on_click=lambda: self.bridge.call(self.bridge.core.resume_all())))
        head.addWidget(button("Add download", "plus", "primary",
                              on_click=lambda: self.actions.add_download_dialog(self)))
        lay.addLayout(head)

        bar = FlowLayout(spacing=6)              # the speed controls wrap below the filters when narrow
        self.filter_group = QButtonGroup(self)
        for key, text in FILTERS:
            b = QPushButton(text)
            b.setProperty("variant", "chip")
            b.setCheckable(True)
            b.setChecked(key == "all")
            b.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            self.filter_group.addButton(b)
            bar.addWidget(b)
        speed = QWidget()
        sl = QHBoxLayout(speed)
        sl.setContentsMargins(16, 0, 0, 0)
        sl.setSpacing(6)
        sl.addWidget(label("At once", "muted"))
        self.concurrency = spin_box()
        self.concurrency.setRange(1, 16)
        self.concurrency.setFixedWidth(70)
        self.concurrency.setToolTip("Maximum number of downloads running at the same time")
        self.concurrency.valueChanged.connect(lambda v: self._set("max_concurrent_downloads", v))
        sl.addWidget(self.concurrency)
        sl.addSpacing(8)
        sl.addWidget(label("Speed limit", "muted"))
        self.limit = spin_box()
        self.limit.setRange(0, 1_000_000)
        self.limit.setSpecialValueText("unlimited")
        self.limit.setSuffix(" KiB/s")
        self.limit.setSingleStep(256)
        self.limit.setFixedWidth(140)
        self.limit.setToolTip("Shared bandwidth limit for direct downloads (torrent limits are in Settings)")
        self.limit.editingFinished.connect(lambda: self._set("http_speed_limit_kbps", self.limit.value()))
        sl.addWidget(self.limit)
        bar.addWidget(speed)
        lay.addLayout(bar)

        # transfers done by the in-app browser after the user's check (cannot resume after the app closes)
        self.browser_card = QFrame()
        bl = QVBoxLayout(self.browser_card)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        bl.addWidget(label("In-app browser downloads", "h2"))
        bl.addWidget(label("Started from a site's own download button after you did its check. They pause and "
                           "resume while the app is open, but cannot continue after it closes.", "small", wrap=True))
        self.browser_rows_box = QVBoxLayout()
        self.browser_rows_box.setSpacing(4)
        bl.addLayout(self.browser_rows_box)
        self.browser_rows: dict[int, BrowserRow] = {}
        self.browser_card.hide()
        lay.addWidget(self.browser_card)
        actions.browser_downloads_changed.connect(self.refresh_browser)

        split = QSplitter(Qt.Vertical)
        self.list = QListWidget()
        self.list.setSpacing(4)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.currentItemChanged.connect(self._selected)
        self.empty = EmptyState("download", "No downloads yet",
                                "Open a game and choose a download method, or add a magnet link, .torrent file or "
                                "direct link with “Add download”.")
        self.list_stack = QWidget()
        sl = QVBoxLayout(self.list_stack)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.list)
        sl.addWidget(self.empty)
        split.addWidget(self.list_stack)
        self.details = QFrame()
        self.details.setProperty("card", True)
        dl = QVBoxLayout(self.details)
        dl.setContentsMargins(16, 12, 16, 12)
        self.d_title = label("", "h2")
        dl.addWidget(self.d_title)
        self.d_info = label("", "muted", wrap=True, selectable=True)
        dl.addWidget(self.d_info)
        self.files = QTreeWidget()
        self.files.setHeaderLabels(["File", "Size", "Progress"])
        self.files.setRootIsDecorated(False)
        self.files.header().setSectionResizeMode(0, QHeaderView.Stretch)
        dl.addWidget(self.files, 1)
        frow = QHBoxLayout()
        self.apply_files = button("Apply file selection", "check", on_click=self._apply_files)
        frow.addWidget(self.apply_files)
        frow.addStretch(1)
        dl.addLayout(frow)
        split.addWidget(self.details)
        split.setSizes([500, 220])
        lay.addWidget(split, 1)
        self.details.hide()
        self._files_for: int | None = None
        self._refresh_empty()

    # ------------------------------------------------------- browser downloads
    def browser(self):
        from ezgd.ui.dialogs import browser
        return browser.manager(self.actions)

    def refresh_browser(self) -> None:
        m = self.browser()
        snaps = {s["id"]: s for s in (m.snapshots() if m else [])}
        for did in [d for d in self.browser_rows if d not in snaps]:
            self.browser_rows.pop(did).deleteLater()
        for did, s in snaps.items():
            row = self.browser_rows.get(did)
            if row is None:
                row = self.browser_rows[did] = BrowserRow(self, s)
                self.browser_rows_box.insertWidget(0, row)
            else:
                row.update_snap(s)
        self.browser_card.setVisible(bool(snaps))
        self._refresh_empty()

    # -------------------------------------------------------------- settings
    def load_settings(self, s: dict) -> None:
        for w, key in ((self.concurrency, "max_concurrent_downloads"), (self.limit, "http_speed_limit_kbps")):
            w.blockSignals(True)
            w.setValue(int(s.get(key, 0)))
            w.blockSignals(False)

    def _set(self, key: str, value) -> None:
        self.bridge.call(self.bridge.core.set_setting(key, value))

    # ----------------------------------------------------------------- data
    def load_all(self) -> None:
        self.bridge.call(self.bridge.core.downloads_snapshot(), self._load_all, owner=self)

    def _load_all(self, snaps: list[dict]) -> None:
        for s in snaps:
            self.upsert(s)

    def upsert(self, snap: dict) -> None:
        tid = snap["id"]
        if tid in self.rows:
            item, row = self.rows[tid]
            row.update_snap(snap)
        else:
            row = DownloadRow(self, snap)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, tid)
            self.list.insertItem(0, item)
            self.list.setItemWidget(item, row)
            self.rows[tid] = (item, row)
            if tid in self.extractions:
                row.set_extraction(self.extractions[tid])
        item.setSizeHint(QSize(100, row.sizeHint().height() + 4))
        item.setHidden(not matches_filter(snap, self.filter_key))
        if self.list.currentItem() is item:
            self._show_details(snap)
        self._refresh_empty()

    def removed(self, tid: int) -> None:
        if tid in self.rows:
            item, _row = self.rows.pop(tid)
            self.list.takeItem(self.list.row(item))
        if self._files_for == tid:
            self.details.hide()
        self._refresh_empty()

    def extraction(self, state: dict) -> None:
        did = state.get("download_id")
        if did is None:
            return
        self.extractions[did] = state
        if did in self.rows:
            item, row = self.rows[did]
            row.set_extraction(state)
            item.setSizeHint(QSize(100, row.sizeHint().height() + 4))

    def set_summary(self, summary: dict) -> None:
        text = f"{summary['active']} active · {summary['queued']} queued · ↓ {format_speed(summary['speed'])}"
        if summary.get("upload"):
            text += f" · ↑ {format_speed(summary['upload'])}"
        if summary.get("torrent_error"):
            text += f" · torrent engine: {summary['torrent_error']}"
        self.summary.setText(text)

    def set_filter(self, key: str) -> None:
        self.filter_key = key
        for item, row in self.rows.values():
            item.setHidden(not matches_filter(row.snap, key))
        self._refresh_empty()

    def _refresh_empty(self) -> None:
        visible = any(not item.isHidden() for item, _ in self.rows.values())
        self.list.setVisible(visible)
        # in-app browser downloads are listed above; no "No downloads yet" under them
        self.empty.setVisible(not visible and not getattr(self, "browser_rows", None))
        if not self.rows:
            self.empty.set("No downloads yet", "Open a game and choose a download method, or use “Add download”.")
        elif not visible:
            self.empty.set("Nothing here", "No downloads match this filter.")

    # -------------------------------------------------------------- actions
    def command(self, name: str, tid: int, *args) -> None:
        self.bridge.call(self.bridge.core.download_command(name, tid, *args), None,
                         lambda e: self.actions.fail(f"Could not {name.replace('_', ' ')}", e))

    def cancel(self, snap: dict) -> None:
        r = QMessageBox.question(self, "Cancel download",
                                 f"Cancel “{snap['title']}”? The partially downloaded data will be deleted.")
        if r == QMessageBox.Yes:
            self.command("cancel", snap["id"], True)

    def remove(self, snap: dict, delete_files: bool) -> None:
        if delete_files:
            target = snap["dest_dir"] + ("/" + snap["filename"] if snap.get("filename") else "")
            r = QMessageBox.warning(self, "Delete downloaded files",
                                    f"Remove “{snap['title']}” and permanently delete its downloaded files?\n\n"
                                    f"{target}\n\nOnly this download's own file/folder is deleted.",
                                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return
        self.bridge.call(self.bridge.core.download_command("remove", snap["id"], delete_files), None,
                         lambda e: self.actions.fail("Could not remove", e))

    def change_destination(self, snap: dict) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose destination", snap["dest_dir"])
        if d:
            self.command("change_destination", snap["id"], d)

    # -------------------------------------------------------------- details
    def _selected(self, current: QListWidgetItem | None, _prev) -> None:
        if current is None:
            self.details.hide()
            return
        tid = current.data(Qt.UserRole)
        if tid in self.rows:
            self._show_details(self.rows[tid][1].snap, rebuild_files=True)

    def _show_details(self, snap: dict, rebuild_files: bool = False) -> None:
        self.details.show()
        self.d_title.setText(snap["title"])
        lines = [f"Source: {theme.source_name(snap['source'])}" if snap.get("source") else "Source: added manually",
                 f"Destination: {snap['dest_dir']}"]
        if snap.get("archive_password"):
            lines.append(f"Archive password: {snap['archive_password']} (used automatically for extraction)")
        if snap.get("filename"):
            lines.append(f"File/folder: {snap['filename']}")
        if snap["kind"] == "torrent" and snap.get("info_hash"):
            lines.append(f"Info hash: {snap['info_hash']}")
        if snap["status"] == "completed":
            lines.append(f"Integrity: {VERIFICATION_TEXT.get(snap['verification'], snap['verification'])}")
        if snap.get("hash_failures"):
            lines.append(f"{snap['hash_failures']} corrupt piece(s) were detected and downloaded again.")
        if snap.get("error"):
            lines.append(f"Error: {snap['error']}")
        self.d_info.setText("\n".join(lines))
        files = snap.get("files") or []
        show_files = snap["kind"] == "torrent" and bool(files)
        self.files.setVisible(show_files)
        self.apply_files.setVisible(show_files and snap["status"] not in ("cancelled",))
        if not show_files:
            return
        if rebuild_files or self._files_for != snap["id"] or self.files.topLevelItemCount() != len(files):
            self._files_for = snap["id"]
            self.files.clear()
            for f in files:
                it = QTreeWidgetItem([f["path"], format_bytes(f["size"]), ""])
                it.setData(0, Qt.UserRole, f["index"])
                it.setCheckState(0, Qt.Checked if f.get("priority", 4) else Qt.Unchecked)
                self.files.addTopLevelItem(it)
        for i, f in enumerate(files):
            it = self.files.topLevelItem(i)
            if it is not None:
                it.setText(2, f"{f.get('progress', 0) * 100:.0f}%" if f.get("priority", 4) else "skipped")

    def _apply_files(self) -> None:
        if self._files_for is None:
            return
        sel = [self.files.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.files.topLevelItemCount())
               if self.files.topLevelItem(i).checkState(0) == Qt.Checked]
        if not sel:
            self.actions.toasts.show("Select at least one file", kind="warning")
            return
        selected = None if len(sel) == self.files.topLevelItemCount() else sel
        self.command("set_files", self._files_for, selected)
        self.actions.toasts.show("File selection updated", kind="success")
