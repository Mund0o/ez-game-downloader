"""Settings: every option is saved immediately and applied live."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QInputDialog, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from ezgd.core.fsutil import format_bytes
from ezgd.core.settings import ACCENTS
from ezgd.ui import theme
from ezgd.ui.dialogs.download_options import folder_row
from ezgd.ui.dialogs.misc import EventsDialog, open_folder
from ezgd.ui.widgets.common import button, card, label, relative_time, spin_box


class SettingsPage(QWidget):
    def __init__(self, actions, paths, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.paths = paths
        self.widgets: dict[str, QWidget] = {}
        self._loading = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(28, 24, 28, 28)
        self.lay.setSpacing(16)
        self.lay.addWidget(label("Settings", "title"))
        self.lay.addWidget(label("Changes are saved immediately.", "muted"))
        self.find = QLineEdit()
        self.find.setObjectName("search")
        self.find.setPlaceholderText("Find a setting… (e.g. proton, speed, couch, password)")
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(self._filter)
        from ezgd.ui.icons import icon
        find_icon = QLabel(self.find)
        find_icon.setPixmap(icon("search", theme.CURRENT.muted, 18).pixmap(18, 18))
        find_icon.move(12, 12)
        self.lay.addWidget(self.find)
        self.no_match = label("No setting matches that.", "muted")
        self.no_match.hide()
        self.lay.addWidget(self.no_match)
        self._sections: list[tuple[QWidget, str, QFormLayout]] = []
        self._build()
        self.lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

    # -------------------------------------------------------------- helpers
    def _section(self, title: str, subtitle: str = "") -> QFormLayout:
        c, cl = card(margins=(18, 16, 18, 16))
        cl.addWidget(label(title, "h2"))
        if subtitle:
            cl.addWidget(label(subtitle, "small", wrap=True))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)
        cl.addLayout(form)
        self.lay.addWidget(c)
        self._sections.append((c, f"{title} {subtitle}".casefold(), form))
        return form

    @staticmethod
    def _words(w) -> str:
        """Everything a row says: labels, check boxes, buttons, choices and tooltips (for Find a setting)."""
        parts = []
        for x in [w] + w.findChildren(QWidget):
            for attr in ("text", "placeholderText", "toolTip"):
                fn = getattr(x, attr, None)
                if callable(fn):
                    try:
                        parts.append(str(fn()))
                    except TypeError:
                        pass
            if isinstance(x, QComboBox):
                parts += [x.itemText(i) for i in range(x.count())]
        return " ".join(parts).casefold()

    def _filter(self, text: str) -> None:
        words = text.casefold().split()
        any_shown = False
        for card_w, head, form in self._sections:
            whole = all(wd in head for wd in words)
            shown = 0
            for row in range(form.rowCount()):
                if not words or whole:
                    ok = True
                else:
                    said = ""
                    for role in (QFormLayout.LabelRole, QFormLayout.FieldRole, QFormLayout.SpanningRole):
                        item = form.itemAt(row, role)
                        if item is None:
                            continue
                        if item.widget() is not None:
                            said += " " + self._words(item.widget())
                        elif item.layout() is not None:
                            lay = item.layout()
                            for k in range(lay.count()):
                                if lay.itemAt(k).widget() is not None:
                                    said += " " + self._words(lay.itemAt(k).widget())
                    ok = all(wd in said for wd in words)
                form.setRowVisible(row, ok)
                shown += ok
            card_w.setVisible(shown > 0 or whole)
            any_shown |= shown > 0 or whole
        self.no_match.setVisible(bool(words) and not any_shown)

    def _save(self, key: str, value) -> None:
        if self._loading:
            return
        self.bridge.call(self.bridge.core.set_setting(key, value), None,
                         lambda e: self.actions.fail(f"Invalid value for {key}", e))

    def _own_overlay_toggled(self, on: bool) -> None:
        """Our overlay: the first time it is switched on, say in two lines what it does, then set the Steam client
        bridge up (Steam restarts once) — the box only stays ticked when that worked."""
        if self._loading:
            return
        box = self.widgets["own_overlay"]
        if not on:
            self._save("own_overlay", False)
            return

        def undo(err=None) -> None:
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)
            if err is not None:
                self.actions.fail("Our overlay could not be switched on", err)

        def go() -> None:
            self.actions.toasts.show("Our overlay", "Setting it up — Steam restarts once…")
            self.bridge.call(self.bridge.core.steam_bridge_setup(),
                             lambda st: (self._save("own_overlay", True),
                                         self.actions.toasts.show("Our overlay is on", "Chats and invites are in the "
                                                                  "quick menu.", "success")) if st.get("ready") else
                             undo(RuntimeError("Steam did not come back with it — start Steam and try again.")),
                             undo, owner=self)

        def checked(st: dict) -> None:
            if st.get("set_up") and st.get("ready"):
                self._save("own_overlay", True)             # set up before: no questions
                return
            from ezgd.platform.steambridge import FIRST_TIME
            r = QMessageBox.question(self, "Our overlay", "\n".join(FIRST_TIME),
                                     QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
            go() if r == QMessageBox.Ok else undo()
        self.bridge.call(self.bridge.core.steam_bridge_status(), checked, undo, owner=self)

    def _check(self, form, key: str, text: str, tip: str = "") -> QCheckBox:
        w = QCheckBox(text)
        if tip:
            w.setToolTip(tip)
        w.toggled.connect(lambda v: self._save(key, v))
        form.addRow("", w)
        self.widgets[key] = w
        return w

    def _spin(self, form, key: str, text: str, lo: int, hi: int, suffix: str = "", special: str = "",
              step: int = 1) -> QSpinBox:
        w = spin_box()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        if suffix:
            w.setSuffix(suffix)
        if special:
            w.setSpecialValueText(special)
        w.editingFinished.connect(lambda: self._save(key, w.value()))
        w.setMaximumWidth(220)
        form.addRow(text, w)
        self.widgets[key] = w
        return w

    def _folder(self, form, key: str, text: str) -> QLineEdit:
        w = QLineEdit()
        w.editingFinished.connect(lambda: self._save(key, w.text().strip()))
        w.textChanged.connect(lambda t: self._save(key, t.strip()) if not w.hasFocus() else None)
        form.addRow(text, folder_row(w, self, text))
        self.widgets[key] = w
        return w

    def _volume(self, form, key: str, text: str, sample: bool) -> QSlider:
        """A 0–100 % slider; ``sample``: play a couch sound at the new volume when it is let go."""
        w = QSlider(Qt.Horizontal)
        w.setRange(0, 100)
        w.setSingleStep(1)
        w.setPageStep(5)
        w.setMaximumWidth(260)
        value = QLabel()
        value.setMinimumWidth(48)
        w.valueChanged.connect(lambda v: value.setText(f"{v} %"))

        def save() -> None:
            self._save(key, w.value())
            if sample and not self._loading:
                from ezgd.ui import couch_sounds
                couch_sounds.preview(w.value())
        w.sliderReleased.connect(save)
        w.valueChanged.connect(lambda _v: None if w.isSliderDown() else save())     # keys / clicks on the track
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(w)
        row.addWidget(value)
        row.addStretch(1)
        box = QWidget()
        box.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        form.addRow(text, box)
        self.widgets[key] = w
        return w

    def _combo(self, form, key: str, text: str, items: list[tuple[str, str]]) -> QComboBox:
        w = QComboBox()
        for value, title in items:
            w.addItem(title, value)
        w.currentIndexChanged.connect(lambda _i: self._save(key, w.currentData()))
        w.setMaximumWidth(260)
        form.addRow(text, w)
        self.widgets[key] = w
        return w

    # ---------------------------------------------------------------- build
    def _cleanup(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        from ezgd.core.fsutil import format_bytes
        core = self.actions.bridge.core

        def got(r: dict) -> None:
            if not r["items"]:
                self.actions.toasts.show("Nothing to clean up", "No archives of extracted games are left in the "
                                         "app's folders.", "success")
                return
            names = "\n".join(f"• {i['game']}: {Path(i['archive']).name}"
                              + (f" (+{len(i['files']) - 1} parts)" if len(i["files"]) > 1 else "")
                              + f" — {format_bytes(i['size'])}" for i in r["items"][:15])
            more = f"\n… and {len(r['items']) - 15} more" if len(r["items"]) > 15 else ""
            if QMessageBox.question(self, "Delete archives",
                                    f"These archives were extracted successfully; the games do not need them any "
                                    f"more:\n\n{names}{more}\n\nDelete them and free "
                                    f"{format_bytes(r['size'])}?") != QMessageBox.Yes:
                return
            self.actions.bridge.call(core.delete_leftover_archives(),
                                     lambda d: self.actions.toasts.show(
                                         "Archives deleted", f"{d['files']} file(s), {format_bytes(d['freed'])} "
                                         "freed.", "success"),
                                     lambda e: self.actions.fail("Could not delete the archives", e), owner=self)
        self.actions.bridge.call(core.leftover_archives(), got,
                                 lambda e: self.actions.fail("Could not list the archives", e), owner=self)

    def _build(self) -> None:
        f = self._section("System check", "External tools for extracting archives, running Windows games and "
                                          "launch-option helpers.")
        self.system_label = label("Checking…", "small", wrap=True)
        f.addRow(self.system_label)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(button("Open system check…", "check", on_click=self._system_check))
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        f.addRow(w)

        f = self._section("Storage", "Where downloads are saved and games are extracted.")
        self._folder(f, "install_dir", "Games folder")
        one = self._check(f, "downloads_in_game_folder", "Use one folder: download each game into its own folder "
                          "here and extract it there",
                          "A download goes into <Games folder>/<game>/ and is extracted into the same folder. With "
                          "“Delete the archive after a successful extraction” on, only the game is left. The separate "
                          "download folder below is then not used.")
        dl = self._folder(f, "download_dir", "Download folder")
        one.toggled.connect(lambda v: dl.parentWidget().setEnabled(not v))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.cleanup_btn = button("Delete archives of installed games…", "close",
                                  tooltip="Archives of games that were extracted successfully but are still on disk "
                                          "(from before “delete the archive” was on)", on_click=self._cleanup)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.cleanup_btn)
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        f.addRow("", w)

        f = self._section("Downloads")
        self._spin(f, "max_concurrent_downloads", "Simultaneous downloads", 1, 16)
        self._spin(f, "http_speed_limit_kbps", "HTTP speed limit", 0, 1_000_000, " KiB/s", "unlimited", 256)
        self._spin(f, "http_max_retries", "Retries per download", 0, 20)
        self._spin(f, "http_timeout_seconds", "Network timeout", 5, 300, " s")
        hours = QCheckBox("Only download during these hours")
        hours.setToolTip("Outside these hours downloads wait in the queue and continue by themselves when the hours "
                         "start (e.g. at night). Downloads you paused yourself stay paused.")
        hours.toggled.connect(lambda v: self._save("download_hours", v))
        self.widgets["download_hours"] = hours
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(hours)
        times = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
        for key, text in (("download_from", "from"), ("download_to", "to")):
            c = QComboBox()
            for t in times:
                c.addItem(t, t)
            c.currentIndexChanged.connect(lambda _i, c=c, key=key: self._save(key, c.currentData()))
            self.widgets[key] = c
            row.addWidget(label(text, "muted"))
            row.addWidget(c)
        row.addStretch(1)
        f.addRow("Download hours", row)
        self._spin(f, "play_speed_limit_kbps", "While a game runs", 0, 1_000_000, " KiB/s", "no change", 256) \
            .setToolTip("Downloads (torrents and direct) go at most this fast while you play, so online games do "
                        "not lag; your normal limits come back when the game ends.")

        f = self._section("Torrents", "The built-in BitTorrent engine (libtorrent). No external client is needed. "
                                      "Actual speed depends on how many peers share a torrent.")
        self._spin(f, "torrent_listen_port", "Listen port", 0, 65535, special="random")
        self._spin(f, "torrent_max_connections", "Max connections", 10, 5000)
        self._spin(f, "torrent_download_limit_kbps", "Download limit", 0, 1_000_000, " KiB/s", "unlimited", 256)
        self._spin(f, "torrent_upload_limit_kbps", "Upload limit", 0, 1_000_000, " KiB/s", "unlimited", 64)
        self._spin(f, "torrent_stall_minutes", "Report stalled after", 1, 240, " min")
        self._check(f, "torrent_enable_dht", "Use DHT (find peers without trackers)")
        self._check(f, "torrent_enable_lsd", "Local peer discovery")
        self._check(f, "torrent_enable_upnp", "Automatic port forwarding (UPnP / NAT-PMP)")
        self._check(f, "torrent_seed_after_complete", "Keep seeding after a torrent completes")

        f = self._section("Extraction", "Archives are extracted safely into a staging folder; nothing is executed.")
        self._check(f, "auto_extract", "Extract archives automatically after downloading")
        self._check(f, "extract_into_install_dir", "Extract into the installation folder (otherwise next to the archive)")
        self._check(f, "delete_archive_after_extract", "Delete the archive after a successful extraction",
                    "Applies to every download — torrents, in-app browser downloads and imported files. Only the "
                    "archive that was extracted (all of its parts) is deleted, and only once the whole archive "
                    "was unpacked without errors.")
        tool = QLineEdit()
        tool.setPlaceholderText("auto-detect unrar / 7z")
        tool.editingFinished.connect(lambda: self._save("unrar_tool", tool.text().strip()))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(tool, 1)
        row.addWidget(button("Browse…", "folder", on_click=lambda: self._pick_tool(tool)))
        w = QWidget()
        w.setLayout(row)
        f.addRow("RAR tool", w)
        self.widgets["unrar_tool"] = tool

        f = self._section("Sources", "Disable a source to hide it from search and listings.")
        self.source_checks: dict[str, QCheckBox] = {}
        for sid, name in (("onlinefix", "OnlineFix (online-fix.me)"), ("steamrip", "SteamRIP (steamrip.com)"),
                          ("crackedgames", "Cracked-Games (cracked-games.org)"),
                          ("fitgirl", "FitGirl Repacks (fitgirl-repacks.site)"),
                          ("ankergames", "AnkerGames (ankergames.net)")):
            c = QCheckBox(name)
            c.toggled.connect(self._sources_changed)
            self.source_checks[sid] = c
            f.addRow("", c)
        self.custom_holder = QVBoxLayout()
        self.custom_holder.setSpacing(4)
        cw = QWidget()
        cw.setLayout(self.custom_holder)
        f.addRow("Your sites", cw)
        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.addWidget(button("Add a site…", "plus", on_click=self._add_site))
        add_row.addWidget(label("Add game download sites like SteamRIP (WordPress sites with a public posts API).",
                                "small", wrap=True), 1)
        aw2 = QWidget()
        aw2.setLayout(add_row)
        f.addRow("", aw2)
        self.bridge.call(self.bridge.core.get_sources(), self.load_custom_sources, owner=self)
        self._check(f, "hide_withdrawn_games", "Hide games whose guide the source has closed",
                    "OnlineFix marks some games “Руководство закрыто” (guide closed) and points to another "
                    "release; those are not shown or offered for download.")
        self._check(f, "browser_adblock", "In-app browser: block ads, pop-ups and pop-unders",
                    "Blocks ad, pop-up and tracker sites (StevenBlack hosts list, MIT licence) and new windows a page "
                    "opens when you click on something else. The switch is also in the browser window.")
        self._check(f, "browser_adblock_auto_update", "Keep the ad blocklist up to date automatically",
                    "Checks GitHub for a newer StevenBlack list once a day (only downloads when it changed). A "
                    "download that does not look like a complete list is ignored.")
        arow = QHBoxLayout()
        arow.setContentsMargins(0, 0, 0, 0)
        self.adblock_info = label("", "small", wrap=True)
        arow.addWidget(self.adblock_info, 1)
        self.adblock_btn = button("Update now", "refresh", "ghost", on_click=self._update_adblock)
        arow.addWidget(self.adblock_btn)
        aw = QWidget()
        aw.setLayout(arow)
        f.addRow("", aw)
        self.bridge.call(self.bridge.core.get_adblock_status(), self._adblock_status, owner=self)
        self._check(f, "hide_adult_games", "Hide games the source marks 18+",
                    "cracked-games.org labels adult games “18+ / NSFW” and blurs them; the app hides them "
                    "unless you untick this.")
        self._spin(f, "cache_ttl_minutes", "Reuse listings for", 1, 10080, " min")

        f = self._section("Library & launching")
        self._check(f, "confirm_before_launch", "Always confirm before launching a game")
        self._check(f, "check_game_updates", "Check installed games for new versions (every few hours)",
                    "Re-reads each installed game's page on its source and tells you when a newer version is out. "
                    "Sites that ask tools not to crawl them (SteamRIP) are only checked when you press “Check for "
                    "updates” in the Library.")
        self._check(f, "auto_update_games", "Install game updates automatically",
                    "When a newer version is out, it downloads by itself (in the app, torrent first) and is installed "
                    "over the game — saves are backed up first. Games that are running, or whose page only has "
                    "browser downloads, are left to you.")
        self._check(f, "auto_backup_saves", "Back up saves automatically (after playing, before updates and "
                                            "uninstalls)",
                    "Zips each game's saves from its Proton prefix (caches and logs left out). Backups are kept per "
                    "game title in the app's data folder, so a reinstall finds them again.")
        sync = self._folder(f, "save_sync_dir", "Synced save folder")
        sync.setPlaceholderText("optional — a Syncthing / Nextcloud / Google Drive folder: your saves on every PC")
        sync.setToolTip("Save backups are written into “EZ Game saves” in this folder, so another computer with the "
                        "same folder (and this app) can restore them. Backups made before stay here too. Leave it "
                        "empty to keep backups on this computer only.")
        own = QCheckBox("Our overlay instead of Steam's: chat, invites and joining in the quick menu")
        own.setToolTip("On: games start without Steam's overlay, and the PS / Xbox / Steam button (tap or hold) opens "
                       "the app's quick menu over the game — your Steam chats, one-click invites to your game, "
                       "joining theirs — through the Steam client itself, without Steam's windows. Off: Steam's "
                       "overlay as usual.")
        own.toggled.connect(self._own_overlay_toggled)
        f.addRow("", own)
        self.widgets["own_overlay"] = own
        self._check(f, "steam_overlay", "Steam overlay in games while Steam is running (friends list)",
                    "Loads Steam's overlay into games started here, without changing anything in Steam. Steam only "
                    "shows the friends list there, and Shift+Tab may not open it again once closed.")
        self._check(f, "launch_through_steam", "Full Steam overlay: add games to Steam and start them through it",
                    "Your games are added to Steam as non-Steam games that start through this app "
                    "(same Proton setup and fixes), so Steam gives them the whole overlay and Steam Input. Steam has "
                    "to restart once for that (the app asks first); the change is verified and backed up, and put "
                    "back automatically if Steam does not start normally. OnlineFix games do not see Steam's game "
                    "ids (they trigger OnlineFix's activation message).")
        for key in ("steam_overlay", "launch_through_steam"):          # (ours replaces Steam's while it is on)
            own.toggled.connect(lambda on, w=self.widgets[key]: w.setEnabled(not on))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(button("Remove the app's games from Steam…", "trash", "ghost",
                             tooltip="Takes out every game this app added to Steam (restarts Steam if it is open)",
                             on_click=self._remove_steam_shortcuts))
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        f.addRow("", w)
        steam_root = QLineEdit()
        steam_root.setPlaceholderText("auto-detect")
        steam_root.editingFinished.connect(lambda: self._save("steam_root", steam_root.text().strip()))
        f.addRow("Steam folder", folder_row(steam_root, self, "Steam folder"))
        self.widgets["steam_root"] = steam_root
        self._steam_friends_row(f)
        self.env_label = label("", "small", wrap=True)
        f.addRow("", self.env_label)
        grow = QHBoxLayout()
        grow.setContentsMargins(0, 0, 0, 0)
        self.ge_info = label("Checking for GE-Proton updates…", "small", wrap=True)
        grow.addWidget(self.ge_info, 1)
        self.ge_btn = button("Install latest GE-Proton", "download", "ghost", on_click=self._install_ge)
        self.ge_btn.setEnabled(False)
        grow.addWidget(self.ge_btn)
        gw = QWidget()
        gw.setLayout(grow)
        f.addRow("GE-Proton", gw)
        self.bridge.call(self.bridge.core.ge_proton_status(), self._ge_status, owner=self)
        self._check(f, "protondb_fixes", "Debugging assistant: use fixes ProtonDB players reported (≈70 MB, monthly)",
                    "Downloads ProtonDB's open report data (ODbL) and lets the assistant try the launch options and "
                    "Proton versions players used to get each game working. Only safe launch options are used.")
        prow = QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        self.pdb_info = label("", "small", wrap=True)
        prow.addWidget(self.pdb_info, 1)
        self.pdb_btn = button("Update now", "refresh", "ghost", on_click=self._update_pdb)
        prow.addWidget(self.pdb_btn)
        pw2 = QWidget()
        pw2.setLayout(prow)
        f.addRow("", pw2)
        self.bridge.call(self.bridge.core.protondb_status(), self._pdb_status, owner=self)

        f = self._section("Interface")
        accents = QHBoxLayout()
        accents.setContentsMargins(0, 0, 0, 0)
        self.accent_group = QButtonGroup(self)
        for key, color in ACCENTS.items():
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(30, 30)
            b.setToolTip(key.title())
            b.setStyleSheet(f"QPushButton {{ background: {color}; border-radius: 15px; border: 2px solid transparent; }}"
                            f"QPushButton:checked {{ border: 3px solid white; }}")
            b.clicked.connect(lambda _=False, k=key: self._save("theme_accent", k))
            b.setProperty("accent", key)
            self.accent_group.addButton(b)
            accents.addWidget(b)
        accents.addStretch(1)
        aw = QWidget()
        aw.setLayout(accents)
        f.addRow("Accent colour", aw)
        self._combo(f, "theme_contrast", "Background", [("dark", "Dark"), ("midnight", "Midnight (darker)")])
        self._combo(f, "card_size", "Game cards", [("compact", "Compact"), ("comfortable", "Comfortable"),
                                                   ("large", "Large")])
        self._check(f, "gpu_acceleration", "GPU acceleration (recommended) — couch mode uses it the next time it "
                                           "opens, the in-app browser after a restart",
                    "Couch mode is drawn by the graphics card (OpenGL) instead of the processor: smoother, especially "
                    "at 4K. The in-app browser draws pages and decodes video on it. Off: the processor does all of "
                    "it. If the graphics driver has a problem with it, the app falls back to the processor by "
                    "itself.")
        self._check(f, "mangohud_live", "MangoHud ready in every game — switch it on or off over a running game "
                                        "(quick menu, Game settings)",
                    "Games start with MangoHud loaded but hidden (when MangoHud is installed), so it can be shown "
                    "at once instead of after a restart. Untick if a game has trouble with MangoHud.")
        self._check(f, "reduce_motion", "Reduce animations")
        self._check(f, "notifications_enabled", "Show desktop notifications when downloads finish or fail")
        self._check(f, "start_with_session", "Start with the computer (in the tray)",
                    "The app starts when you sign in, as an icon in the tray: downloads finish and the wishlist and "
                    "game updates are checked without opening the window.")

        f = self._section("Couch mode display", "Couch mode fills the screen the app is on by itself. If it does not "
                                                "fit your setup, choose the screen, a borderless window or the exact "
                                                "size here (also in couch mode: Settings → Couch mode display).")
        from PySide6.QtGui import QGuiApplication
        from ezgd.ui.window_control import real_size
        screens = [("", "The app's screen")]
        for sc in QGuiApplication.screens():
            pw, ph = real_size(sc)
            screens.append((sc.name(), f"{sc.name()} · {pw}×{ph}"))
        self._combo(f, "couch_screen", "Screen", screens)
        self._combo(f, "couch_window", "Window", [("fullscreen", "Full screen"), ("borderless", "Borderless window"),
                                                  ("custom", "Custom size")])
        res = [("", "The screen's size")] + [(r, r.replace("x", " × ")) for r in (
            "1280x720", "1600x900", "1920x1080", "2560x1080", "2560x1440", "3440x1440", "3840x1600", "3840x2160",
            "5120x1440")]
        self._combo(f, "couch_resolution", "Custom size", res)
        self._combo(f, "couch_ui_scale", "Interface size", [(v, f"{v} %") for v in (75, 90, 100, 110, 125, 150,
                                                                                       175, 200)])
        self._check(f, "couch_intro", "Intro video when couch mode opens (2 seconds; any button skips it)")
        self._check(f, "couch_intro_sound", "Intro sound")
        self._volume(f, "couch_intro_volume", "Intro volume", sample=False)
        self._check(f, "couch_sounds", "Sounds in couch mode (quiet: moving the selector, pressing into something, "
                                       "clicking in the browser)")
        self._volume(f, "couch_sounds_volume", "Sound volume", sample=True)
        self._check(f, "couch_steady_frames", "Steady frame rate (stops flicker on VRR / FreeSync / G-Sync screens)")
        self._check(f, "couch_controller_only", "Couch mode's Store: only games that work with a controller "
                                                "(full or partial controller support on Steam)")

        f = self._section("Cache & logs")
        self.cache_label = label("", "small")
        f.addRow("", self.cache_label)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(button("Clear cache", "trash", on_click=self._clear_cache))
        row.addWidget(button("Open log folder", "folder", on_click=lambda: open_folder(self.paths.logs)))
        row.addWidget(button("Recent errors…", "warning", on_click=self._events))
        row.addWidget(button("Crash reports…", "bug", tooltip="The last crash's error code and report, to copy and "
                                                               "send", on_click=self._crash_reports))
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        f.addRow("", w)
        self._combo(f, "log_level", "Log level", [(x, x.title()) for x in ("DEBUG", "INFO", "WARNING", "ERROR")])

        f = self._section("Database", "Settings, cached catalog, download queue and library live in a SQLite "
                                      "database. A backup is made automatically before every schema migration and "
                                      "when the app exits.")
        self.backup_label = label("", "small", wrap=True)
        f.addRow("", self.backup_label)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(button("Back up now", "check", on_click=self._backup))
        row.addWidget(button("Restore from backup…", "retry", on_click=self._restore))
        row.addWidget(button("Open data folder", "folder", on_click=lambda: open_folder(self.paths.data)))
        row.addStretch(1)
        w = QWidget()
        w.setLayout(row)
        f.addRow("", w)

        f = self._section("App updates", "New versions of EZ Game Downloader come from a GitHub releases page. "
                                         "Downloads are checked against the SHA-256 GitHub publishes before anything "
                                         "is installed; the previous version is kept until the new one has started.")
        rrow = QHBoxLayout()
        rrow.setContentsMargins(0, 0, 0, 0)
        self.repo_edit = QLineEdit()
        from ezgd.core.selfupdate import DEFAULT_REPO
        self.repo_edit.setPlaceholderText(DEFAULT_REPO)
        self.repo_edit.returnPressed.connect(self._save_repo)
        rrow.addWidget(self.repo_edit, 1)
        rrow.addWidget(button("Save", "check", on_click=self._save_repo))
        rw = QWidget()
        rw.setLayout(rrow)
        f.addRow("GitHub repository", rw)
        self._check(f, "check_app_updates", "Look for a new version once a day")
        self.app_update_info = label("", "small", wrap=True)
        f.addRow("", self.app_update_info)
        from PySide6.QtWidgets import QProgressBar
        self.app_update_bar = QProgressBar()
        self.app_update_bar.setRange(0, 1000)
        self.app_update_bar.hide()
        f.addRow("", self.app_update_bar)
        urow = QHBoxLayout()
        urow.setContentsMargins(0, 0, 0, 0)
        self.app_check_btn = button("Check now", "refresh", "ghost", on_click=self._check_app_update)
        urow.addWidget(self.app_check_btn)
        self.app_get_btn = button("Download update", "download", "primary", on_click=self._download_app_update)
        self.app_get_btn.hide()
        urow.addWidget(self.app_get_btn)
        self.app_install_btn = button("Restart and update", "update", "primary", on_click=self._install_app_update)
        self.app_install_btn.hide()
        urow.addWidget(self.app_install_btn)
        self.app_page_btn = button("Release page", "external", "ghost", on_click=self._open_release_page)
        self.app_page_btn.hide()
        urow.addWidget(self.app_page_btn)
        urow.addStretch(1)
        uw = QWidget()
        uw.setLayout(urow)
        f.addRow("", uw)
        self._app_update: dict = {}
        self.bridge.call(self.bridge.core.app_update_status(), self._app_status, owner=self)

        f = self._section("About")
        from ezgd import __version__
        f.addRow("", label(f"EZ Game Downloader {__version__}. Respect copyright and the terms of the sites you use. "
                           f"Downloaded executables are never run automatically.", "small", wrap=True))

    # ----------------------------------------------------------------- load
    def load(self, s: dict, info: bool = True) -> None:
        """Show these values (``info``: also refresh the system / cache / backup lines, which ask the core)."""
        self._loading = True
        try:
            for key, w in self.widgets.items():
                v = s.get(key)
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(v))
                elif isinstance(w, (QSpinBox, QSlider)):
                    w.setValue(int(v or 0))
                elif isinstance(w, QLineEdit):
                    if not w.hasFocus():
                        w.setText(str(v or ""))
                elif isinstance(w, QComboBox):
                    w.setCurrentIndex(max(0, w.findData(v)))
            for sid, c in self.source_checks.items():
                c.setChecked(bool(s.get("sources_enabled", {}).get(sid, True)))
            for b in self.accent_group.buttons():
                b.setChecked(b.property("accent") == s.get("theme_accent"))
        finally:
            self._loading = False
        if info:
            self.refresh_info()

    # ----------------------------------------------------------- Steam friends
    def _steam_friends_row(self, f: QFormLayout) -> None:
        """The user's own Steam Web API key: friends' live status in the quick menu and couch mode's Friends tab."""
        from ezgd.platform.steamfriends import KEY_PAGE
        key = QLineEdit()
        key.setEchoMode(QLineEdit.Password)
        key.setPlaceholderText("Your Steam Web API key")
        key.editingFinished.connect(lambda: (self._save("steam_web_api_key", key.text().strip()),
                                             self._check_friends()))
        self.widgets["steam_web_api_key"] = key
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(key, 1)
        from ezgd.ui.dialogs.misc import open_url
        row.addWidget(button("Get a key", "external", "ghost", tooltip=KEY_PAGE, on_click=lambda: open_url(KEY_PAGE)))
        row.addWidget(button("Check", "refresh", "ghost", on_click=self._check_friends))
        w = QWidget()
        w.setLayout(row)
        f.addRow("Steam friends", w)
        self.friends_info = label("Who is online and what they play — in the quick menu and couch mode's Friends tab. "
                                  "Steam gives every account one key: sign in at steamcommunity.com/dev/apikey, enter "
                                  "any domain name (e.g. localhost) and paste the key here. It stays on this computer "
                                  "and is only sent to Steam.", "small", wrap=True)
        f.addRow("", self.friends_info)

    def _check_friends(self) -> None:
        if self._loading:
            return
        self.friends_info.setText("Asking Steam…")

        def got(d: dict) -> None:
            fr = d.get("friends") or []
            if d.get("live"):
                online = sum(1 for x in fr if x.get("state") not in ("offline", "unknown"))
                text = f"Working: {len(fr)} friends, {online} online now."
                if d.get("private"):
                    text += " (Your friends list is private on Steam: the friends Steam keeps here are used.)"
            else:
                text = d.get("error") or "No live status."
            self.friends_info.setText(text)

        self.bridge.call(self.bridge.core.steam_friends(True), got,
                         lambda e: self.friends_info.setText(str(e)), owner=self)

    # ----------------------------------------------------------- app updates
    def _save_repo(self) -> None:
        def done(repo: str) -> None:
            self.repo_edit.setText(repo)
            self.actions.toasts.show("Saved", f"Updates come from github.com/{repo}.", "success")
            self._check_app_update()
        self.bridge.call(self.bridge.core.set_update_repo(self.repo_edit.text()), done,
                         lambda e: self.actions.fail("GitHub repository", e), owner=self)

    def _app_status(self, st: dict) -> None:
        self._app_update = st
        if not self.repo_edit.hasFocus():
            self.repo_edit.setText(st.get("repo") or "")
        latest = st.get("latest") or {}
        lines = [f"You have version {st.get('current')}."]
        if latest:
            if latest.get("no_release"):
                lines.append("No version has been published yet.")
            elif latest.get("newer"):
                lines.append(f"Version {latest['version']} is available"
                             + (f" (published {str(latest.get('published') or '')[:10]})." if latest.get("published")
                                else "."))
            else:
                lines.append("That is the newest version.")
            if st.get("last_check"):
                from ezgd.ui.widgets.common import relative_time
                lines.append(f"Checked {relative_time(st['last_check'])}.")
        if not st.get("packaged"):
            lines.append("This copy runs from source code, so it cannot replace itself — use git, or a packaged "
                         "build.")
        elif not st.get("can_install"):
            lines.append("Installing automatically is only available on Linux for now; the release page has the "
                         "download.")
        self.app_update_info.setText(" ".join(lines))
        newer = bool(latest.get("newer"))
        self.app_get_btn.setVisible(newer and bool(st.get("can_install")) and not st.get("staged"))
        self.app_install_btn.setVisible(bool(st.get("staged")) and bool(st.get("can_install")))
        self.app_page_btn.setVisible(bool(latest.get("page")))

    def app_update_event(self, name: str, payload) -> None:
        if name == "app_update":
            self.bridge.call(self.bridge.core.app_update_status(), self._app_status, owner=self)
            return
        st = (payload or {}).get("status")
        if st == "downloading":
            total = payload.get("total") or 0
            self.app_update_bar.show()
            self.app_update_bar.setValue(int(1000 * payload["received"] / total) if total else 0)
            self.app_update_info.setText(f"Downloading the update… {format_bytes(payload['received'])}"
                                         + (f" of {format_bytes(total)}" if total else ""))
        elif st == "unpacking":
            self.app_update_bar.setValue(1000)
            self.app_update_info.setText("Checking the new version (SHA-256 verified) and unpacking it…")
        elif st in ("ready", "failed"):
            self.app_update_bar.hide()
            self.bridge.call(self.bridge.core.app_update_status(), self._app_status, owner=self)

    def _check_app_update(self) -> None:
        self.app_check_btn.setEnabled(False)

        def done(info: dict) -> None:
            self.app_check_btn.setEnabled(True)
            self.actions.toasts.show("App update", f"Version {info['version']} is available." if info["newer"] else
                                     f"You have the newest version ({info['current']})."
                                     + (" No newer version has been published yet." if info.get("no_release") else ""),
                                     "success" if info["newer"] else "info")
            self.bridge.call(self.bridge.core.app_update_status(), self._app_status, owner=self)

        def failed(e) -> None:
            self.app_check_btn.setEnabled(True)
            self.actions.fail("Could not check for an app update", e)
        self.bridge.call(self.bridge.core.check_app_update(True), done, failed, owner=self)

    def _download_app_update(self) -> None:
        self.app_get_btn.setEnabled(False)

        def done(info: dict) -> None:
            self.app_get_btn.setEnabled(True)
            self.actions.toasts.show("Update ready", f"Version {info['version']} is downloaded and verified. Press "
                                     "“Restart and update”.", "success")
            self.bridge.call(self.bridge.core.app_update_status(), self._app_status, owner=self)

        def failed(e) -> None:
            self.app_get_btn.setEnabled(True)
            self.app_update_bar.hide()
            self.actions.fail("Could not download the update", e)
        self.bridge.call(self.bridge.core.download_app_update(), done, failed, owner=self)

    def _install_app_update(self) -> None:
        import sys
        r = QMessageBox.question(self, "Restart and update",
                                 "The app closes, swaps in the new version and starts again. Downloads pause and "
                                 "resume after the restart. Continue?")
        if r != QMessageBox.Yes:
            return

        def go(_r) -> None:
            win = self.window()
            if hasattr(win, "force_quit"):
                win.force_quit()
        self.bridge.call(self.bridge.core.install_app_update(sys.argv[1:]), go,
                         lambda e: self.actions.fail("Could not install the update", e), owner=self)

    def _open_release_page(self) -> None:
        from ezgd.ui.dialogs.misc import open_url
        page = (self._app_update.get("latest") or {}).get("page")
        if page:
            open_url(page)

    def _pdb_status(self, st: dict) -> None:
        if st.get("available"):
            self.pdb_info.setText(f"ProtonDB: {st['reports']:,} reports for {st['games']:,} games "
                                  f"({st.get('source', '').replace('reports_', '').replace('.tar.gz', '')}). "
                                  "Data from ProtonDB.com (ODbL).")
        else:
            self.pdb_info.setText("ProtonDB data not downloaded yet — it is fetched in the background, or press "
                                  "Update now.")

    def _update_pdb(self) -> None:
        self.pdb_btn.setEnabled(False)
        self.pdb_btn.setText("Downloading…")

        def done(st: dict) -> None:
            self.pdb_btn.setEnabled(True)
            self.pdb_btn.setText("Update now")
            self._pdb_status(st)
            self.actions.toasts.show("ProtonDB data " + ("updated" if st.get("result") == "updated" else "is up to date"),
                                     f"{st.get('reports', 0):,} reports.", "success")
        self.bridge.call(self.bridge.core.update_protondb(), done,
                         lambda e: (self.pdb_btn.setEnabled(True), self.pdb_btn.setText("Update now"),
                                    self.actions.fail("ProtonDB", e)), owner=self)

    def _ge_status(self, st: dict) -> None:
        have = st.get("newest_installed")
        if st.get("newer_available"):
            from ezgd.core.fsutil import format_bytes
            self.ge_info.setText(f"{st['latest']} is available ({format_bytes(st['size'])})"
                                 + (f"; you have {have}." if have else "; none installed yet."))
            self.ge_btn.setEnabled(True)
            self.ge_btn.setText(f"Install {st['latest']}")
        elif st.get("latest"):
            self.ge_info.setText(f"Up to date: {have or st['latest']}.")
        else:
            self.ge_info.setText("Could not check GitHub for GE-Proton (offline?).")

    def _install_ge(self) -> None:
        self.ge_btn.setEnabled(False)
        self.ge_btn.setText("Downloading…")
        self.bridge.call(self.bridge.core.install_ge_proton(),
                         lambda name: (self.actions.toasts.show(f"{name} installed", "Choose it per game in "
                                                                "Properties, or let Diagnose test it.", "success"),
                                       self.bridge.call(self.bridge.core.ge_proton_status(), self._ge_status,
                                                        owner=self)),
                         lambda e: (self.ge_btn.setEnabled(True), self.actions.fail("GE-Proton", e)), owner=self)

    def _adblock_status(self, st: dict) -> None:
        when = lambda ts: relative_time(ts) if ts else "never"          # noqa: E731
        text = f"Ad blocklist: {st.get('count', 0):,} domains · list from {(st.get('date') or 'unknown')[:17]} · " \
               f"{'downloaded update' if st.get('source') == 'downloaded' else 'bundled with the app'} · " \
               f"checked {when(st.get('checked_at'))}"
        if st.get("filters_available"):
            text += (f"\nFilter lists: {st.get('filter_rules', 0):,} rules from {st.get('filter_lists', 0)} lists "
                     "(EasyList, EasyPrivacy, uBlock filters/badware/unbreak, Peter Lowe) — ads, pop-ups and "
                     "page elements")
        if st.get("last_error"):
            text += f" · last check failed: {st['last_error']}"
        self.adblock_info.setText(text)

    def _update_adblock(self) -> None:
        self.adblock_btn.setEnabled(False)
        self.adblock_btn.setText("Checking…")

        def done(st: dict) -> None:
            self.adblock_btn.setEnabled(True)
            self.adblock_btn.setText("Update now")
            self._adblock_status(st)
            self.actions.toasts.show("Ad blocklist " + ("updated" if st.get("result") == "updated" else "is up to date"),
                                     f"{st.get('count', 0):,} domains · {st.get('filter_rules', 0):,} filter rules.",
                                     "success")

        def failed(err: BaseException) -> None:
            self.adblock_btn.setEnabled(True)
            self.adblock_btn.setText("Update now")
            self.actions.fail("Ad blocklist", err)
            self.bridge.call(self.bridge.core.get_adblock_status(), self._adblock_status, owner=self)
        self.bridge.call(self.bridge.core.update_adblock(), done, failed, owner=self)

    def _system_check(self) -> None:
        from ezgd.ui.dialogs.system import SystemCheckDialog
        SystemCheckDialog(self.actions, self).exec()
        self.refresh_info()

    def _system(self, st: dict) -> None:
        missing = [t["name"] + (" (highly recommended)" if t["importance"] == "highly_recommended" else "")
                   for t in st["tools"] if not t["installed"]]
        self.system_label.setText("All tools are installed." if not missing else
                                  f"Missing: {', '.join(missing)}.")

    def refresh_info(self) -> None:
        self.bridge.call(self.bridge.core.system_status(), self._system, owner=self)
        self.bridge.call(self.bridge.core.cache_stats(), self._cache_stats, owner=self)
        self.bridge.call(self.bridge.core.list_backups(), self._backups, owner=self)
        self.bridge.call(self.bridge.core.library_environment(), self._env, owner=self)

    def _cache_stats(self, st: dict) -> None:
        self.cache_label.setText(f"{st['games']} cached games · page cache {format_bytes(st['http_cache_bytes'])} · "
                                 f"covers {format_bytes(st['cover_bytes'])}")

    def _backups(self, backups: list[dict]) -> None:
        if backups:
            latest = backups[0]
            self.backup_label.setText(f"{len(backups)} backup(s) · latest {Path(latest['path']).name} "
                                      f"({relative_time(latest['mtime'])})")
        else:
            self.backup_label.setText("No backups yet.")

    def _env(self, env: dict) -> None:
        parts = []
        parts.append(f"Steam: {env['steam_root']}" if env.get("steam_root") else "Steam: not found")
        if env.get("linux"):
            parts.append("Proton: " + (", ".join(env["protons"]) if env.get("protons") else "none found"))
            parts.append("Wine: " + ("installed" if env.get("wine") else "not installed"))
        self.env_label.setText(" · ".join(parts))

    # ---------------------------------------------------------- your sites
    def load_custom_sources(self, sources: list[dict]) -> None:
        from ezgd.ui.widgets.common import clear_layout
        clear_layout(self.custom_holder)
        for sid in [k for k in self.source_checks if k.startswith("site:")]:
            self.source_checks.pop(sid)
        custom = [s for s in sources if s.get("custom")]
        if not custom:
            self.custom_holder.addWidget(label("None yet.", "small"))
            return
        enabled = self.actions.settings.get("sources_enabled", {})
        for s in custom:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            c = QCheckBox(f"{s['name']} ({s['homepage'].split('://', 1)[-1]})")
            c.setChecked(bool(enabled.get(s["id"], True)))
            c.toggled.connect(self._sources_changed)
            self.source_checks[s["id"]] = c
            row.addWidget(c, 1)
            d = s.get("details") or {}
            if d.get("posts_checked"):
                row.addWidget(label(f"{d.get('posts_with_downloads')}/{d.get('posts_checked')} posts with downloads "
                                    "when added", "small"))
            row.addWidget(button("Remove", "trash", "ghost", on_click=lambda sid=s["id"], n=s["name"]:
                                 self._remove_site(sid, n)))
            w = QWidget()
            w.setLayout(row)
            self.custom_holder.addWidget(w)

    def _add_site(self) -> None:
        from ezgd.ui.dialogs.add_site import open_add_site
        r = open_add_site(self.actions, self)
        if r:
            self.actions.toasts.show(f"{r['name']} added", "Its games now appear in All Games and search.", "success")

    def _remove_site(self, sid: str, name: str) -> None:
        if QMessageBox.question(self, "Remove site", f"Remove {name} from your sources? Games already in your "
                                "Library stay there.") != QMessageBox.Yes:
            return
        self.bridge.call(self.bridge.core.remove_custom_source(sid),
                         lambda _r: self.actions.toasts.show(f"{name} removed", kind="success"),
                         lambda e: self.actions.fail("Could not remove the site", e), owner=self)

    # -------------------------------------------------------------- actions
    def _sources_changed(self, _v: bool) -> None:
        if self._loading:
            return
        self._save("sources_enabled", {sid: c.isChecked() for sid, c in self.source_checks.items()})

    def _pick_tool(self, edit: QLineEdit) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Choose unrar or 7z executable", str(Path.home()))
        if f:
            edit.setText(f)
            self._save("unrar_tool", f)

    def _remove_steam_shortcuts(self) -> None:
        r = QMessageBox.question(self, "Remove from Steam",
                                 "Remove every game this app added to Steam? Your other Steam games and shortcuts are "
                                 "not touched. If Steam is open it is closed first (it rewrites its list on exit).")
        if r != QMessageBox.Yes:
            return
        self.bridge.call(self.bridge.core.library_call("remove_steam_shortcuts", True),
                         lambda n: self.actions.toasts.show("Removed from Steam", f"{n} game(s) taken out of Steam. "
                                                            "Start Steam again when you like.", "success"),
                         lambda e: self.actions.fail("Remove from Steam", e))

    def _crash_reports(self) -> None:
        from ezgd.core import crashreport
        from ezgd.ui.dialogs.crash import show_report
        reports = crashreport.latest_reports(self.paths.logs)
        if not reports:
            QMessageBox.information(self, "Crash reports", "No crashes have been recorded. 🎉")
            return
        items = [crashreport.load_report(p) for p in reports]
        if len(items) == 1:
            show_report(items[0], self)
            return
        names = [f"{r.path.stem[6:21].replace('-', ' ', 1)} — {r.code} — {r.summary[:70]}" for r in items]
        choice, ok = QInputDialog.getItem(self, "Crash reports", "Newest first:", names, 0, False)
        if ok:
            show_report(items[names.index(choice)], self)

    def _clear_cache(self) -> None:
        r = QMessageBox.question(self, "Clear cache", "Delete cached web pages and cover images? Your library, "
                                                      "downloads and settings are kept.")
        if r == QMessageBox.Yes:
            self.bridge.call(self.bridge.core.clear_cache(), lambda _r: (self.actions.toasts.show("Cache cleared",
                                                                                                    kind="success"),
                                                                         self.refresh_info()))

    def _events(self) -> None:
        self.bridge.call(self.bridge.core.recent_events(), lambda ev: EventsDialog(ev, self).exec(), owner=self)

    def _backup(self) -> None:
        self.bridge.call(self.bridge.core.backup_database(),
                         lambda p: (self.actions.toasts.show("Database backed up", p, "success"), self.refresh_info()),
                         lambda e: self.actions.fail("Backup failed", e))

    def _restore(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Choose a backup", str(self.paths.backups),
                                           "SQLite backups (*.sqlite3)")
        if not f:
            return
        r = QMessageBox.warning(self, "Restore database",
                                "Restoring replaces the current settings, catalog cache, download list and library "
                                "with the backup. Downloads are stopped and the app will close; start it again "
                                "afterwards. The current database is backed up first.\n\nContinue?",
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r != QMessageBox.Yes:
            return

        def done(_):
            QMessageBox.information(self, "Restore complete", "The backup was restored. The app will now close.")
            self.window().force_quit()
        self.bridge.call(self.bridge.core.restore_database(f), done, lambda e: self.actions.fail("Restore failed", e))
