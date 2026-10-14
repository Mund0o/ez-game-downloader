"""User flows shared by several pages (download, launch, report)."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from ezgd.sources.base import ManualActionRequired
from ezgd.ui import theme
from ezgd.ui.dialogs.download_options import DownloadOptionsDialog
from ezgd.ui.dialogs.misc import AddDownloadDialog, ConfirmLaunchDialog, ReportDialog, open_folder, open_url

log = logging.getLogger(__name__)


def error_text(err: BaseException) -> str:
    return str(err) or type(err).__name__


class Actions(QObject):
    navigate = Signal(str)            # page key
    download_added = Signal(dict)
    browser_downloads_changed = Signal()   # in-app browser transfers (ezgd.ui.dialogs.browser)

    def __init__(self, bridge, covers, toasts, settings: dict, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.core = bridge.core
        self.covers = covers
        self.toasts = toasts
        self.settings = settings
        self.installed_keys: set[str] = set()

    # ------------------------------------------------------------ downloads
    def start_download(self, parent: QWidget, game_id: str, method_index: int, on_done=None,
                       on_manual=None) -> None:
        self.toasts.show("Preparing download…", "Fetching the download reference from the source.")

        def prepared(prep: dict) -> None:
            dlg = DownloadOptionsDialog(prep, self.settings, parent)
            if dlg.exec() != DownloadOptionsDialog.Accepted:
                return
            v = dlg.values()
            self.bridge.call(self.core.start_prepared(prep["token"], v["dest_dir"], selected_files=v["selected_files"],
                                                      auto_extract=v["auto_extract"], extract_dest=v["extract_dest"]),
                             lambda snap: self._added(snap, on_done), lambda e: self.fail("Could not start", e))

        def failed(err: BaseException) -> None:
            if isinstance(err, ManualActionRequired):
                if on_manual:
                    on_manual()               # e.g. re-render the game page: the method is now browser-only
                self.manual(parent, str(err), err.url)
            else:
                self.fail("Could not prepare the download", err)
        self.bridge.call(self.core.prepare_download(game_id, method_index), prepared, failed, owner=parent)

    def start_download_set(self, parent: QWidget, game_id: str, indices: list[int], label_text: str) -> None:
        """All parts of a multi-part archive: confirm the options once, then queue every part with them."""
        if not indices:
            return
        self.toasts.show("Preparing download…", f"{label_text}: {len(indices)} parts.")

        def queue_rest(v: dict, rest: list[int], queued: list[dict]) -> None:
            if not rest:
                self.toasts.show("All parts queued", f"{len(queued)} parts — the game is extracted once the last "
                                                     "part has downloaded.", "success")
                for s in queued:
                    self.download_added.emit(s)
                return

            def prepared(prep: dict) -> None:
                self.bridge.call(self.core.start_prepared(prep["token"], v["dest_dir"], auto_extract=v["auto_extract"],
                                                          extract_dest=v["extract_dest"]),
                                 lambda snap: queue_rest(v, rest[1:], queued + [snap]),
                                 lambda e: self.fail(f"Could not queue part {len(queued) + 1}", e))
            self.bridge.call(self.core.prepare_download(game_id, rest[0]), prepared,
                             lambda e: self.fail(f"Could not prepare part {len(queued) + 1}", e))

        def first(prep: dict) -> None:
            dlg = DownloadOptionsDialog(prep, self.settings, parent)
            if dlg.exec() != DownloadOptionsDialog.Accepted:
                return
            v = dlg.values()
            self.bridge.call(self.core.start_prepared(prep["token"], v["dest_dir"], selected_files=v["selected_files"],
                                                      auto_extract=v["auto_extract"], extract_dest=v["extract_dest"]),
                             lambda snap: queue_rest(v, indices[1:], [snap]),
                             lambda e: self.fail("Could not start", e))
        self.bridge.call(self.core.prepare_download(game_id, indices[0]), first,
                         lambda e: self.fail("Could not prepare the download", e), owner=parent)

    def _added(self, snap: dict, on_done) -> None:
        self.toasts.show("Added to downloads", snap["title"], "success")
        self.download_added.emit(snap)
        if on_done:
            on_done(snap)

    def manual(self, parent: QWidget, reason: str, url: str) -> None:
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Browser required")
        box.setText("This download has to be completed in your web browser.")
        box.setInformativeText(f"{reason}\n\nAfter the browser gives you the final file link you can paste it in "
                               f"Downloads → Add download, or import the downloaded archive in Library.")
        open_btn = box.addButton("Open in browser", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            open_url(url)

    def import_downloaded(self, parent: QWidget, game, on_done=None) -> None:
        """Pick a file the user downloaded in the browser and add it to the library for ``game``."""
        start = Path.home() / "Downloads"
        if not start.is_dir():
            start = Path(self.settings.get("download_dir") or Path.home())
        path, _ = QFileDialog.getOpenFileName(
            parent, f"Choose the file you downloaded for {game.title}", str(start),
            "Archives (*.zip *.7z *.rar *.tar *.gz *.xz *.bz2 *.001);;All files (*)")
        if not path:
            return
        if game.extra.get("sha256"):
            self.toasts.show("Checking the file…", "Comparing its SHA-256 with the value the site publishes.")

        def done(r: dict) -> None:
            how = {True: "SHA-256 matches the site's published value.", None: "The site publishes no checksum.",
                   False: "Imported without a matching checksum."}[r["verified"]]
            pw = game.extra.get("archive_password") or theme.source_password(game.source)
            if r["extracting"]:
                how += f" Extracting now with the password “{pw}”…" if pw else " Extracting now…"
            elif pw:
                how += f" Archive password: {pw}"
            self.toasts.show("Imported into the library", f"{r['title']} — {how}", "success")
            if on_done:
                on_done(r)

        def failed(err: BaseException, retry=True) -> None:
            from ezgd.core.app_core import ChecksumMismatchError
            if not isinstance(err, ChecksumMismatchError) or not retry:
                self.fail("Could not import", err)
                return
            box = QMessageBox(parent)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Checksum does not match")
            box.setText("This file is not the one published on the game's page.")
            box.setInformativeText(f"{err}\n\nRe-download it, or import it anyway if you are sure (for example a "
                                   "newer build than the page lists).")
            anyway = box.addButton("Import anyway", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is anyway:
                self.bridge.call(self.core.import_downloaded_file(game.id, path, verify=False), done,
                                 lambda e: failed(e, False))
        self.bridge.call(self.core.import_downloaded_file(game.id, path), done, failed)

    def add_download_dialog(self, parent: QWidget, initial: str = "") -> None:
        dlg = AddDownloadDialog(self.settings, parent, initial)
        if dlg.exec() != AddDownloadDialog.Accepted:
            return
        v = dlg.values()
        if v["torrent"]:
            coro = self.core.add_torrent_file(v["torrent"], v["dest_dir"], auto_extract=v["auto_extract"])
        else:
            coro = self.core.add_link(v["link"], v["dest_dir"], v["title"], v["auto_extract"])
        self.bridge.call(coro, lambda snap: self._added(snap, None),
                         lambda e: self.fail("Could not add the download", e))

    # --------------------------------------------------------------- launch
    def launch(self, parent: QWidget, entry, *, confirm=None, couch: bool = False, started=None,
               failed=None) -> None:
        """Play ``entry``. Couch mode passes ``confirm(entry, plan, proceed)`` (its own full-screen question instead
        of a desktop dialog), ``couch=True`` (never asks to restart Steam: plays right away) and callbacks for when
        the game was started or could not be."""
        def go() -> None:
            self.bridge.call(self.core.library_call("steam_overlay_status", entry.id),
                             lambda st: self._start(parent, entry, st, couch, started, failed),
                             lambda _e: self._start(parent, entry, {"use": False, "steam_running": True}, couch,
                                                    started, failed))

        def got_plan(plan) -> None:
            need = (not entry.executable_trusted) or self.settings.get("confirm_before_launch", True)
            if need and confirm is not None:
                confirm(entry, plan, go)
                return
            if need:
                dlg = ConfirmLaunchDialog(entry.title, entry.executable, plan.description, entry.executable_trusted,
                                          parent, before=[c.display() for c in plan.pre],
                                          after=[c.display() for c in plan.post], script=plan.shell_script)
                if dlg.exec() != ConfirmLaunchDialog.Accepted:
                    return
            go()

        def no_plan(e: BaseException) -> None:
            self.fail("Cannot launch yet", e)
            if failed:
                failed(e)
        self.bridge.call(self.core.library_call("launch_plan", entry.id), got_plan, no_plan, owner=parent)

    def _start(self, parent: QWidget, entry, st: dict, couch: bool = False, started=None, failed=None) -> None:
        """Play: through Steam when it can be (the Steam overlay only appears in games Steam starts), else directly."""
        def ok_started() -> None:
            if started:
                started(entry)

        def not_started(e: BaseException) -> None:
            self.fail("Could not launch", e)
            if failed:
                failed(e)

        def direct(note: str = "") -> None:
            if st.get("needs_steam") and not st.get("steam_running"):
                st["steam_running"] = True          # once
                self.toasts.show("Starting Steam first", f"{entry.title} uses OnlineFix, which needs Steam running "
                                 "and signed in — the game starts as soon as Steam is up.", "info")
                self.bridge.call(self.core.library_call("start_steam"),
                                 lambda ok: direct(note) if ok else (
                                     self.toasts.show("Steam did not start", "Starting the game anyway — if it "
                                                      "does not go online, start Steam and press Play again.",
                                                      "warning"), direct(note)),
                                 lambda e: direct(note))
                return
            self.bridge.call(self.core.library_call("launch", entry.id, True),
                             lambda p: (self.toasts.show("Game started", entry.title + (f" — {note}" if note else ""),
                                                         "success"), ok_started()),
                             not_started)

        def via_steam(_r=None) -> None:
            self.bridge.call(self.core.library_call("launch_via_steam", entry.id),
                             lambda _p: (self.toasts.show("Starting through Steam",
                                                          f"{entry.title} — with the Steam overlay "
                                                          "(Shift+Tab in the game).", "success"), ok_started()),
                             lambda e: (self.toasts.show("Steam did not take it", f"{e} — starting it directly.",
                                                         "warning"), direct()))

        if not st.get("use"):
            return direct()
        if st.get("ready"):
            return via_steam()
        if couch and st.get("steam_running"):
            # couch mode never closes or restarts Steam: the game starts with the friends-list overlay
            return direct()
        if not st.get("steam_running"):          # Steam is closed: add the games to it now, then start through it
            self.bridge.call(self.core.library_call("prepare_steam_shortcuts", False), via_steam,
                             lambda e: (self.fail("Could not add the game to Steam", e), direct()))
            return
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Steam overlay")
        box.setText(f"Start “{entry.title}” with the full Steam overlay?")
        box.setInformativeText(
            "The full overlay (Shift+Tab any time: friends, chat, browser, screenshots, Steam Input) needs Steam to "
            "start the game itself, so the app adds your games to Steam as non-Steam games — they still start with "
            "this app's Proton setup and fixes. Steam only reads that list when it starts, so it is closed and "
            "opened again now. Steam's list is checked and backed up first, and put back automatically if Steam "
            "does not start normally afterwards.\n\n"
            "Close any game that is running in Steam first.")
        restart = box.addButton("Restart Steam and play (full overlay)", QMessageBox.AcceptRole)
        without = box.addButton("Just play (friends list only)", QMessageBox.RejectRole)
        never = box.addButton("Never start games through Steam", QMessageBox.DestructiveRole)
        box.setDefaultButton(restart)
        box.exec()
        clicked = box.clickedButton()
        if clicked is restart:
            self.toasts.show("Restarting Steam…", "Adding your games to Steam, then starting "
                             f"{entry.title} through it.", "info")
            self.bridge.call(self.core.library_call("prepare_steam_shortcuts", True), via_steam,
                             lambda e: (self.fail("Could not add the game to Steam", e), direct()))
        elif clicked is never:
            self.settings["launch_through_steam"] = False
            self.bridge.call(self.core.set_setting("launch_through_steam", False))
            direct("you can switch the Steam overlay back on in Settings")
        elif clicked is without:
            direct()

    # --------------------------------------------------------------- report
    def report(self, parent: QWidget, source: str, url: str | None, details: str) -> None:
        dlg = ReportDialog(source, url, details, parent)
        if dlg.exec() != ReportDialog.Accepted:
            return
        v = dlg.values()

        def saved(path: str) -> None:
            box = QMessageBox(parent)
            box.setWindowTitle("Report saved")
            box.setText(f"The report was saved to:\n{path}")
            open_btn = box.addButton("Open folder", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is open_btn:
                open_folder(path)
        self.bridge.call(self.core.report_problem(v["source"], v["kind"], v["url"], v["message"], v["comment"]),
                         saved, lambda e: self.fail("Could not save the report", e))

    # --------------------------------------------------------------- errors
    def fail(self, title: str, err: BaseException) -> None:
        log.info("%s: %s", title, err)
        self.toasts.show(title, error_text(err), "error")
