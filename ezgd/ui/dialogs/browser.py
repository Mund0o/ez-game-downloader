"""In-app browser for browser-only downloads.

The pop-up is only there for the human part: the user completes any check or captcha and clicks the site's own
download button. As soon as a download starts the pop-up hides itself; the transfer continues in the background
(shown on the Downloads page) and the finished files are added to the library automatically.

Nothing is automated or faked: it is a real Chromium (QtWebEngine) driven by the user, and the transfer is done
by that same browser (its pass/cookies are never copied into another client). Cookies and cache live in memory
only (off-the-record profile, discarded when the app exits); pop-ups to other sites are blocked and sites cannot
use the camera, microphone, location or notifications.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLineEdit, QMessageBox, QVBoxLayout

from ezgd.net import adblock, filterlists
from ezgd.ui.widgets.common import Banner, button, label

log = logging.getLogger(__name__)

try:                                    # optional component (PySide6-WebEngine)
    from PySide6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    AVAILABLE = True
except ImportError:                     # pragma: no cover - depends on the installed Qt modules
    AVAILABLE = False

_PROFILE = None
_PAGES: "weakref.WeakSet | None" = None
_MANAGER: "BrowserDownloads | None" = None


def _host(url: QUrl | str) -> str:
    h = (QUrl(url).host() if isinstance(url, QUrl) else urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _site(host: str) -> str:
    """Registrable part of a host (``a.b.example.com`` → ``example.com``); IP addresses stay whole."""
    import ipaddress
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class AdBlockState:
    """Read from Qt WebEngine's IO thread: only immutable values are swapped in, never mutated."""

    def __init__(self):
        self.enabled = True
        self.allowed: frozenset[str] = frozenset()      # sites the user is using right now
        self.counts: dict[str, int] = {}                 # first-party site → requests blocked
        self.lock = threading.Lock()

    def count(self, site: str) -> int:
        with self.lock:
            return self.counts.get(site, 0)

    def add_allowed(self, *sites: str) -> None:
        self.allowed = frozenset(self.allowed | {s for s in sites if s})


ADBLOCK = AdBlockState()
_INTERCEPTOR = None


def profile():
    """One in-memory profile per app run: a check passed once is not repeated for every download."""
    global _PROFILE, _PAGES, _INTERCEPTOR
    if _PROFILE is None:
        _PROFILE = QWebEngineProfile()              # no storage name → off-the-record (memory only)
        _PROFILE.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
        _PROFILE.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        _PAGES = weakref.WeakSet()
        _INTERCEPTOR = _AdBlocker()
        _PROFILE.setUrlRequestInterceptor(_INTERCEPTOR)
        _PROFILE.scripts().insert(_click_recorder())
        _PROFILE.scripts().insert(_cosmetic_collector())
    return _PROFILE


# remembers what the user actually pressed (link / form target) so a new window that goes somewhere else — the
# "pop-under" a page script opens on any click — can be told apart from the user's own new-tab link. Pop-under
# scripts usually fire on mouse/pointer *down*, before the click, so presses are recorded too. It runs in an
# isolated world: page scripts cannot see or switch off its listeners or its console.
_CLICK_JS = r"""
(function () {
  if (window.__ezgdClicks) return; window.__ezgdClicks = true;
  var dbg = console.debug.bind(console);
  function rec(href) { try { dbg('__ezgd_click__ ' + (href || '')); } catch (e) {} }
  function target(t) {
    var a = t && t.closest ? t.closest('a[href]') : null;
    if (a) return a.href;
    var f = t && t.closest ? t.closest('form') : null;
    return f && f.action ? f.action : '';
  }
  ['pointerdown', 'mousedown', 'touchstart', 'auxclick', 'click'].forEach(function (ev) {
    window.addEventListener(ev, function (e) { rec(target(e.target)); }, true);
  });
  window.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') rec(target(document.activeElement));
  }, true);
  window.addEventListener('submit', function (e) { var f = e.target; rec(f && f.action ? f.action : ''); }, true);
})();
"""


def _script(name: str, source: str, world, sub_frames: bool, point=None):
    from PySide6.QtWebEngineCore import QWebEngineScript
    s = QWebEngineScript()
    s.setName(name)
    s.setSourceCode(source)
    s.setInjectionPoint(point if point is not None else QWebEngineScript.DocumentCreation)
    s.setWorldId(world)
    s.setRunsOnSubFrames(sub_frames)
    return s


def _click_recorder():
    from PySide6.QtWebEngineCore import QWebEngineScript
    return _script("ezgd-click-recorder", _CLICK_JS, QWebEngineScript.ApplicationWorld, True)


def _cosmetic_collector():
    from PySide6.QtWebEngineCore import QWebEngineScript
    return _script("ezgd-cosmetic-collector", filterlists.COLLECT_JS, QWebEngineScript.ApplicationWorld, False)


# programs are never what a game page hands out; on file hosts they are "download" ads
BLOCKED_DOWNLOADS = (".exe", ".msi", ".scr", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jar", ".apk", ".dmg",
                     ".pkg", ".deb", ".rpm", ".appimage", ".sh", ".lnk", ".reg")
GESTURE_SECONDS = 2.5          # a new window this soon after a press counts as the result of that press
_FILE_URL = re.compile(r"\.(rar|zip|7z|tar|gz|xz|bz2|zst|iso|bin|\d{3}|r\d{2}|part\d+\.rar)(?:$|[?#])", re.I)


def _looks_like_file(url: QUrl) -> bool:
    """A direct file link (what a host's download button finally goes to)."""
    return bool(_FILE_URL.search(url.path() or ""))      # by extension only: "/download-now" pages are ads


def _real_href(href: str, current: QUrl) -> str:
    """The pressed link's target, or "" when it is a button in disguise (``#``, ``javascript:``, same page)."""
    if not href or href.lower().startswith(("javascript:", "about:")):
        return ""
    if href.split("#", 1)[0] == current.toString().split("#", 1)[0]:
        return ""                                   # href="#…": scripts decide where it goes
    return href
PROBATION_SECONDS = 6          # a blank pop-up must go somewhere acceptable within this time
HANG_SECONDS = 8               # a page whose script does not answer for this long is offered to be stopped
HANG_STOP_SECONDS = 15         # … and is stopped by itself after this long

if AVAILABLE:
    from PySide6.QtWebEngineCore import QWebEngineScript, QWebEngineSettings, QWebEngineUrlRequestInterceptor

    class _AdBlocker(QWebEngineUrlRequestInterceptor):
        """Drops ad / pop-under / tracker / malware requests: the domain list plus EasyList-style filter lists.

        Qt WebEngine calls this on the GUI thread, so the filter engine is only ever used from one thread."""

        def interceptRequest(self, info):
            if not ADBLOCK.enabled:
                return
            url = info.requestUrl()
            host = url.host()
            if not host:
                return
            rtype = int(info.resourceType().value)
            if rtype in (0, 19):
                return                           # whole pages: decided in acceptNavigationRequest (with a notice)
            first = info.firstPartyUrl()
            hit = ("domain list" if adblock.is_blocked(host, ADBLOCK.allowed) else filterlists.blocked(
                url.toString(), first.toString(), filterlists.QT_TYPES.get(rtype, "other")))
            if hit:
                info.block(True)
                log.debug("blocked %s (%s)", url.toString()[:160], hit)
                site = _site(_host(first) or host)
                with ADBLOCK.lock:
                    ADBLOCK.counts[site] = ADBLOCK.counts.get(site, 0) + 1

    class _Page(QWebEnginePage):
        """A tab in the pop-up. New windows the user clicked open as another tab; script pop-ups are blocked.

        Nothing a page does can lock the window: alert/confirm/prompt never open modal boxes (their text is shown
        in the notice bar), full-screen requests are refused and log-in boxes are cancelled."""

        def __init__(self, prof, owner):
            super().__init__(prof, owner)
            self.owner = owner
            self.probation: tuple[str, float] | None = None   # blank pop-up: (href pressed, deadline)
            self.cosm_exceptions: set[str] = set()
            self.cosm_generic = True
            self.cosm_sent: set[str] = set()
            self.permissionRequested.connect(lambda perm: perm.deny())     # camera, location, notifications…
            self.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
            self.settings().setAttribute(QWebEngineSettings.FullScreenSupportEnabled, False)
            self.newWindowRequested.connect(lambda req: owner._new_window(req, self))
            self.fullScreenRequested.connect(lambda req: req.reject())
            self.windowCloseRequested.connect(lambda: owner._close_tab(self))
            self.renderProcessTerminated.connect(lambda status, code: owner._page_crashed(self, status))
            self.authenticationRequired.connect(lambda _url, _auth: owner._show_notice(
                "The page asked for a user name and password — cancelled (download sites never need one)."))
            self.proxyAuthenticationRequired.connect(lambda *_a: None)
            for name in ("fileSystemAccessRequested", "registerProtocolHandlerRequested"):
                sig = getattr(self, name, None)
                if sig is not None:
                    sig.connect(lambda req: req.reject())

        # --- page dialogs: shown in the notice bar, never modal
        def javaScriptAlert(self, origin, message):
            self.owner._page_message(self, message)

        def javaScriptConfirm(self, origin, message):
            self.owner._page_message(self, message, asked=True)
            return False

        def javaScriptPrompt(self, origin, message, default):
            self.owner._page_message(self, message, asked=True)
            return False, ""

        def javaScriptConsoleMessage(self, level, message, line, source):
            if message.startswith("__ezgd_click__ "):
                self.owner._clicked(message[len("__ezgd_click__ "):])
            elif message.startswith("__ezgd_cosm__ "):
                self.owner._cosmetic(self, message[len("__ezgd_cosm__ "):])
            # page console output is not logged

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if is_main_frame:
                verdict = self.owner._navigation(self, url, nav_type)
                if verdict is not None:
                    self.owner._show_notice(verdict)
                    return False
                self.owner._prepare_cosmetics(self, url)
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)


# ------------------------------------------------------------------ transfers
@dataclass
class BrowserDownload:
    id: int
    req: object
    game: object
    host: str
    session: "BrowserDownloadDialog"
    started: float = field(default_factory=time.monotonic)
    speed: float = 0.0
    _last: tuple[float, int] = (0.0, 0)
    library: str = ""                   # "", "adding", "added", "failed: …"
    final: dict | None = None           # copied when finished: Qt deletes the request with its page

    def freeze(self) -> None:
        r = self.req
        state = {QWebEngineDownloadRequest.DownloadCompleted: "completed",
                 QWebEngineDownloadRequest.DownloadCancelled: "cancelled"}.get(r.state(), "failed")
        self.final = {"name": r.downloadFileName(), "path": Path(r.downloadDirectory()) / r.downloadFileName(),
                      "state": state, "received": r.receivedBytes(), "total": r.totalBytes(),
                      "error": r.interruptReasonString() if state == "failed" else ""}
        self.speed = 0.0

    @property
    def name(self) -> str:
        return self.final["name"] if self.final else self.req.downloadFileName()

    @property
    def path(self) -> Path:
        return self.final["path"] if self.final else Path(self.req.downloadDirectory()) / self.req.downloadFileName()

    @property
    def state(self) -> str:
        if self.final:
            return self.final["state"]
        return "paused" if self.req.isPaused() else "downloading"

    @property
    def error(self) -> str:
        return self.final["error"] if self.final else ""

    def snapshot(self) -> dict:
        if self.final:
            total, got = self.final["total"], self.final["received"]
        else:
            total, got = self.req.totalBytes(), self.req.receivedBytes()
        eta = (total - got) / self.speed if self.speed > 0 and total > 0 else None
        return {"id": self.id, "name": self.name, "game": self.game.title if self.game else "", "host": self.host,
                "state": self.state, "received": got, "total": total if total > 0 else None, "speed": self.speed,
                "eta": eta, "error": self.error, "library": self.library, "path": str(self.path)}


def _plain_name(name: str) -> str:
    """``Game (1).rar`` → ``game.rar``: the browser renames a file it is already saving."""
    return re.sub(r"\s*\(\d{1,2}\)(?=\.[^.]+$|$)", "", name or "").casefold()


class BrowserDownloads(QObject):
    """Owns every in-app browser transfer for the app's lifetime (pop-ups come and go)."""

    changed = Signal()

    def __init__(self, actions):
        super().__init__(actions)
        self.actions = actions
        self.items: dict[int, BrowserDownload] = {}
        self._next = 1
        self._entries: dict[str, int] = {}          # game id → library entry created from browser downloads
        profile().downloadRequested.connect(self._requested)
        self.changed.connect(actions.browser_downloads_changed.emit)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    @property
    def dest(self) -> Path:
        st = self.actions.settings
        if st.get("downloads_in_game_folder", True) and st.get("install_dir"):
            return Path(st["install_dir"]) / "Browser downloads"      # one folder for everything
        return Path(st.get("download_dir") or Path.home() / "Downloads") / "Browser downloads"

    def active(self) -> int:
        return sum(1 for d in self.items.values() if d.state in ("downloading", "paused"))

    def _session_for(self, page) -> "BrowserDownloadDialog | None":
        while page is not None:
            if isinstance(page, _Page):
                return page.owner
            page = page.parent() if isinstance(page.parent(), QWebEnginePage) else None
        return None

    def _requested(self, req) -> None:
        session = self._session_for(req.page())
        if session is None:
            req.cancel()                              # not started from one of our pop-ups
            return
        if req.downloadFileName().lower().endswith(BLOCKED_DOWNLOADS):
            req.cancel()                              # a program, not a game archive: almost always an ad
            session.blocked_download(req.downloadFileName())
            return
        name, url = _plain_name(req.downloadFileName()), req.url().toString()
        if any(d.final is None and (d.req.url().toString() == url or _plain_name(d.req.downloadFileName()) == name)
               for d in self.items.values()):
            req.cancel()                              # already downloading it (a double click, a page asking twice)
            log.info("browser download: %s is already downloading — the second request was dropped", name)
            return
        self.dest.mkdir(parents=True, exist_ok=True)
        req.setDownloadDirectory(str(self.dest))
        req.accept()
        d = BrowserDownload(self._next, req, session.game, _host(req.url()) or session.host_label, session)
        self._next += 1
        self.items[d.id] = d
        req.isFinishedChanged.connect(lambda d=d: self._finished(d))
        req.isPausedChanged.connect(self.changed.emit)
        self._timer.start()
        session.download_started(d)
        self.changed.emit()

    def _tick(self) -> None:
        now = time.monotonic()
        for d in self.items.values():
            if d.state == "downloading" and d.final is None:
                t0, b0 = d._last
                got = d.req.receivedBytes()
                if t0:
                    rate = (got - b0) / max(now - t0, 0.001)
                    d.speed = rate if not d.speed else 0.7 * d.speed + 0.3 * rate
                d._last = (now, got)
            else:
                d.speed = 0.0
        if not self.active():
            self._timer.stop()
        self.changed.emit()

    def _finished(self, d: BrowserDownload) -> None:
        d.freeze()
        self.changed.emit()
        if d.state == "completed" and d.game is not None:
            # each file goes to the library as soon as it is done; parts of a multi-part archive are
            # collected into one entry and extracted once the last part is in
            self._add_to_library(d.game, [d])
        elif d.state == "failed":
            self.actions.toasts.show("Browser download failed", f"{d.name}: {d.error}", "error")
        session = d.session
        if not any(x.state in ("downloading", "paused") for x in self.items.values() if x.session is session):
            session.downloads_settled()

    def _add_to_library(self, game, items: list[BrowserDownload]) -> None:
        for x in [x for x in items if x.path.suffix.lower() == ".torrent" and x.path.is_file()]:
            items = [i for i in items if i is not x]
            self._start_torrent(game, x)          # a site's .torrent file: the built-in engine downloads the game
        if not items:
            return
        runnable = (".exe", ".msi", ".bat", ".cmd", ".sh")
        files = [x for x in items if x.path.is_file() and x.path.suffix.lower() not in runnable]
        for x in items:
            if x not in files:
                x.library = "not added (not an archive)"
        if not files:
            self.changed.emit()
            return
        for x in files:
            x.library = "adding"
        self.changed.emit()
        core = self.actions.core

        def ok(r: dict) -> None:
            self._entries[game.id] = r["entry_id"]
            if r.get("parts") and not r.get("complete"):
                for x in files:
                    x.library = f"part added — {r['parts']}"
                self.actions.toasts.show(f"{r['title']}: {r['parts']}",
                                         "Download the remaining parts; the game is extracted when the last one "
                                         "is in.", "info")
            else:
                for x in files:
                    x.library = "added"
                how = "SHA-256 matches the site's published value. " if r.get("verified") else ""
                self.actions.toasts.show("Added to your Library", f"{r['title']} — {how}"
                                         + ("Extracting now…" if r.get("extracting") else ""), "success")
            self.changed.emit()

        def failed(err: BaseException) -> None:
            for x in files:
                x.library = f"failed: {err}"
            self.actions.fail("Could not add to the Library", err)
            self.changed.emit()
        from ezgd.installation.volumes import volume_of
        entry_id = self._entries.get(game.id)
        if entry_id is not None and not any(volume_of(x.path) for x in files):   # e.g. the fix, later
            coro = self._extract_into(entry_id, [str(x.path) for x in files])
        else:
            coro = core.import_downloaded_files(game.id, [str(x.path) for x in files])
        self.actions.bridge.call(coro, ok, failed)

    def _start_torrent(self, game, x: BrowserDownload) -> None:
        x.library = "starting the torrent"
        self.changed.emit()

        def ok(_task) -> None:
            # the game now downloads as an ordinary torrent in Downloads: the finished .torrent file was a second
            # row for the same game there ("it shows twice" after a couch-mode download)
            self.items.pop(x.id, None)
            self.actions.toasts.show("Torrent started", f"{game.title} is downloading with the built-in torrent "
                                                        "engine — see Downloads.", "success")
            self.changed.emit()

        def failed(err: BaseException) -> None:
            x.library = f"failed: {err}"
            self.actions.fail("Could not start the torrent", err)
            self.changed.emit()
        self.actions.bridge.call(self.actions.core.add_browser_torrent(game.id, str(x.path)), ok, failed)

    async def _extract_into(self, entry_id: int, paths: list[str]) -> dict:
        core = self.actions.core
        e = core.library.get(entry_id)
        for p in paths:
            await core.extract_entry(entry_id, p)
        return {"entry_id": entry_id, "title": e.title, "verified": None, "extracting": False}

    # commands from the Downloads page
    def _running(self, did: int):
        d = self.items.get(did)
        return d.req if d is not None and d.final is None else None

    def pause(self, did: int) -> None:
        if (r := self._running(did)) is not None:
            r.pause()

    def resume(self, did: int) -> None:
        if (r := self._running(did)) is not None:
            r.resume()
            self._timer.start()

    def cancel(self, did: int) -> None:
        if (r := self._running(did)) is not None:
            r.cancel()

    def remove(self, did: int) -> None:
        d = self.items.get(did)
        if d is not None and d.final is not None:
            del self.items[did]
            self.changed.emit()

    def snapshots(self) -> list[dict]:
        return [d.snapshot() for d in self.items.values()]

    def cancel_all(self) -> None:
        for d in self.items.values():
            if d.final is None:
                d.req.cancel()


def manager(actions) -> BrowserDownloads | None:
    global _MANAGER
    if not AVAILABLE:
        return None
    import shiboken6
    if _MANAGER is None or not shiboken6.isValid(_MANAGER) or _MANAGER.actions is not actions:
        if _MANAGER is not None and shiboken6.isValid(_MANAGER):
            try:
                profile().downloadRequested.disconnect(_MANAGER._requested)
            except (RuntimeError, TypeError):
                pass
        _MANAGER = BrowserDownloads(actions)          # one per main window (normally exactly one per run)
    return _MANAGER


# --------------------------------------------------------------------- pop-up
class BrowserDownloadDialog(QDialog):
    """Only for the user's part: the check and the click. Hides itself as soon as a download starts."""

    went_away = Signal()            # hidden (a download started) or closed — couch mode comes back then

    def __init__(self, actions, game, url: str, host_label: str, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.game = game
        self.host_label = host_label
        self.start_url = url
        self.allowed_sites = {_site(_host(url)), _site(_host(game.url))}
        ADBLOCK.enabled = bool(actions.settings.get("browser_adblock", True))
        ADBLOCK.add_allowed(*self.allowed_sites)
        self._last_click: tuple[str, float] = ("", 0.0)
        self._popups_blocked = 0
        self._pending: list = []                     # blank pop-ups waiting to show where they go
        self._ping_sent = 0.0
        self._ping_page = None
        self._hang_shown = False
        self.manager = manager(actions)
        self.started: list[BrowserDownload] = []
        self.setWindowTitle(f"{host_label} — {game.title}")
        self.resize(1100, 820)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        self._banner = Banner("info", "Do the “I'm not a robot” check if one appears, then click the site's own "
                                      "download button. This window closes by itself once the download starts — it "
                                      "continues on the Downloads page and is added to your Library when done.")
        lay.addWidget(self._banner)
        self.zoom = 1.0
        from PySide6.QtWidgets import QWidget
        self._bar_widget = QWidget(self)
        bar = QHBoxLayout(self._bar_widget)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(button("", "retry", "ghost", tooltip="Back", on_click=self.back))
        bar.addWidget(button("", "refresh", "ghost", tooltip="Reload", on_click=lambda: self.current.triggerAction(QWebEnginePage.Reload)))
        self.address = QLineEdit(url)
        self.address.setStyleSheet("font-family: monospace;")
        self.address.setToolTip("You can paste a link here and press Enter")
        self.address.returnPressed.connect(self._go)
        bar.addWidget(self.address, 1)
        bar.addWidget(button("Open in my browser", "external", "ghost",
                             tooltip="Open this page in your normal browser instead; afterwards use “Import "
                                     "downloaded file…” on the game page",
                             on_click=self._external))
        self.shield = QCheckBox("Ad blocker")
        self.shield.setChecked(ADBLOCK.enabled)
        self.shield.setToolTip("Blocks ad, pop-up and tracker sites (StevenBlack hosts list) and pop-unders that "
                               "open when you click on a page. Turn it off if a site does not work, then reload.")
        self.shield.toggled.connect(self._toggle_adblock)
        bar.addWidget(self.shield)
        self.shield_count = label("", "small")
        bar.addWidget(self.shield_count)
        bar.addWidget(button("Cancel", variant="ghost", on_click=self.close))
        lay.addWidget(self._bar_widget)
        self._shield_timer = QTimer(self)
        self._shield_timer.setInterval(1000)
        self._shield_timer.timeout.connect(self._update_shield)
        self._shield_timer.start()
        self._watch = QTimer(self)
        self._watch.setInterval(2500)
        self._watch.timeout.connect(self._watchdog)
        self._watch.start()
        self.notice = Banner("warning", "")
        self.notice.hide()
        lay.addWidget(self.notice)
        # one view per tab, switched in a stack: a page is never moved to another view (re-attaching a page whose
        # navigation was blocked while it was in the background crashes Qt WebEngine)
        from PySide6.QtWidgets import QStackedWidget
        self.stack = QStackedWidget(self)
        self._views: dict = {}
        self._closed: list = []                       # tabs left with Back: kept until the window goes
        self.page = _Page(profile(), self)
        _PAGES.add(self.page)
        self.tabs: list = [self.page]                 # pages opened by the user's clicks, newest last
        self._attach(self.page)
        lay.addWidget(self.stack, 1)
        self.view.load(QUrl(url))

    # ------------------------------------------------------------ navigation
    @property
    def view(self):
        return self.stack.currentWidget()

    @property
    def current(self):
        return self.view.page()

    def _attach(self, page):
        v = QWebEngineView(self.stack)
        v.setPage(page)
        if self.zoom != 1.0:
            v.setZoomFactor(self.zoom)
        for sig in (page.loadStarted, page.loadFinished):
            sig.connect(lambda *_a, page=page: self._page_loading(page))
        v.urlChanged.connect(lambda u, v=v: self.address.setText(u.toString()) if v is self.stack.currentWidget()
                             else None)
        self.stack.addWidget(v)
        self._views[page] = v
        return v

    def _switch_to(self, page) -> None:
        v = self._views.get(page) or self._attach(page)
        self.stack.setCurrentWidget(v)
        self.address.setText(page.url().toString())

    def _drop(self, page) -> None:
        """Delete a tab and its view (never detached from each other first)."""
        v = self._views.pop(page, None)
        page.deleteLater()
        if v is not None:
            self.stack.removeWidget(v)
            v.deleteLater()

    def _clicked(self, href: str) -> None:
        self._last_click = (href, time.monotonic())

    def _gesture(self) -> tuple[str, bool]:
        """(href of the last press, whether it was recent enough to explain a new window)."""
        href, at = self._last_click
        return href, time.monotonic() - at <= GESTURE_SECONDS

    def _acceptable(self, url: QUrl, opener: QUrl, user_initiated: bool) -> tuple[bool, str]:
        """May a new window / pop-up tab go to ``url``? (reason when not)"""
        site = _site(_host(url))
        if ADBLOCK.enabled and (adblock.is_blocked(url.host(), ADBLOCK.allowed)
                                or filterlists.popup_blocked(url.toString(), opener.toString())):
            return False, "ad blocker"
        if site in self.allowed_sites:
            return True, ""
        if not user_initiated:
            return False, "opened by the page, not by you"
        if not ADBLOCK.enabled:
            return True, ""
        href, recent = self._gesture()
        if not recent:
            return False, "opened by the page without you pressing a link to it"
        href = _real_href(href, opener)
        if _looks_like_file(url):
            return True, ""                           # the file itself, right after the user's press
        if href and (QUrl(href) == url or _site(_host(QUrl(href))) == site):
            return True, ""                           # the link / form the user pressed
        if not href and site == _site(_host(opener)):
            return True, ""                           # a button on this site opening this site
        return False, "a pop-under: the page opened it when you clicked something else"

    def _new_window(self, req, opener_page=None) -> None:
        """A link/button that opens a new tab: follow it if the user clicked it; block script pop-ups."""
        url = req.requestedUrl()
        opener = (opener_page or self.current).url()
        blank = url.isEmpty() or url.scheme() in ("about", "") or url.toString() in ("about:blank", "about:srcdoc")
        if blank:
            # window.open('') and then sent somewhere by script: decided when it navigates
            if not req.isUserInitiated() and ADBLOCK.enabled:
                return self._blocked_popup(url, "opened by the page, not by you")
            tab = _Page(profile(), self)
            _PAGES.add(tab)
            tab.probation = (self._gesture()[0], time.monotonic() + PROBATION_SECONDS)
            req.openIn(tab)
            self._pending.append(tab)
            QTimer.singleShot(int(PROBATION_SECONDS * 1000) + 200, lambda t=tab: self._expire_probation(t))
            return
        wanted, why = self._acceptable(url, opener, req.isUserInitiated())
        if wanted:
            self._open_tab(req, url)
        else:
            self._blocked_popup(url, why)

    def _open_tab(self, req, url: QUrl) -> None:
        self.allowed_sites.add(_site(_host(url)))
        ADBLOCK.add_allowed(_site(_host(url)))
        tab = _Page(profile(), self)
        _PAGES.add(tab)
        req.openIn(tab)                          # keeps POST data / opener, e.g. a host's download form
        self._show_tab(tab, url)

    def _show_tab(self, tab, url: QUrl | None = None) -> None:
        if tab not in self.tabs:
            self.tabs.append(tab)
        self._switch_to(tab)
        if not tab.url().toString() and url is not None:
            self.address.setText(url.toString())

    def _blocked_popup(self, url: QUrl, why: str) -> None:
        log.info("blocked pop-up to %s (%s)", url.toString()[:200], why)
        self._popups_blocked += 1
        target = _site(_host(url)) or "a blank page"
        self._show_notice(f"Blocked a pop-up to {target} — {why}.", "Open it" if not url.isEmpty() else None,
                          (lambda u=url: self._open(u)) if not url.isEmpty() else None)

    def _expire_probation(self, tab) -> None:
        import shiboken6
        if shiboken6.isValid(tab) and tab.probation is not None and tab in self._pending:
            self._pending.remove(tab)             # a blank pop-up that never went anywhere
            tab.deleteLater()

    def _navigation(self, page, url: QUrl, nav_type) -> str | None:
        """Main-frame navigation check; returns the notice text when it is blocked."""
        if url.scheme() not in ("http", "https"):
            return None
        if page.probation is not None:             # a blank pop-up going somewhere: was it the user's link?
            href, deadline = page.probation
            page.probation = None
            if page in self._pending:
                self._pending.remove(page)
            self._last_click = (href, time.monotonic()) if time.monotonic() <= deadline else ("", 0.0)
            ok, why = self._acceptable(url, self.current.url(), True)
            if not ok:
                QTimer.singleShot(0, page.deleteLater)
                self._popups_blocked += 1
                log.info("blocked pop-up to %s (%s)", url.toString()[:200], why)
                return f"Blocked a pop-up to {_site(_host(url))} — {why}."
            self.allowed_sites.add(_site(_host(url)))
            ADBLOCK.add_allowed(_site(_host(url)))
            QTimer.singleShot(0, lambda: self._show_tab(page, url))
            return None
        if not ADBLOCK.enabled:
            return None
        site = _site(_host(url))
        if site in self.allowed_sites:
            return None
        current = page.url()
        if adblock.is_blocked(url.host(), ADBLOCK.allowed) or filterlists.popup_blocked(url.toString(),
                                                                                         current.toString()):
            self._popups_blocked += 1
            return f"Blocked a redirect to {site} (ad blocker)."
        # "tab-under" / swapped link: right after the user pressed a link to one site, the page goes to another
        # (Chromium reports such script navigations as link clicks, so the pressed link is what tells them apart)
        if nav_type not in (QWebEnginePage.NavigationTypeLinkClicked, QWebEnginePage.NavigationTypeOther,
                            QWebEnginePage.NavigationTypeFormSubmitted):
            return None                             # typed, back/forward, reload, server redirects
        href, recent = self._gesture()
        href = _real_href(href, current)
        if _looks_like_file(url):
            return None                             # a download button's file link (often via href="#" + script)
        if recent and href and _site(_host(QUrl(href))) != site and _site(_host(current)) != site:
            self._popups_blocked += 1
            log.info("blocked a script redirect to %s after a press on %s", url.toString()[:200], href[:200])
            return f"Blocked a redirect to {site}: the page sent you there when you clicked something else."
        return None

    # --------------------------------------------------- element hiding
    def _prepare_cosmetics(self, page, url: QUrl) -> None:
        """Hide this site's known ad elements as soon as its next page starts (specific filter rules)."""
        page.cosm_sent = set()
        coll = page.scripts()
        for old in coll.find("ezgd-cosmetic-site"):
            coll.remove(old)
        if not ADBLOCK.enabled:
            page.cosm_generic = False
            return
        selectors, exceptions, generichide = filterlists.cosmetic(url.toString())
        page.cosm_exceptions = exceptions
        page.cosm_generic = not generichide
        if selectors:
            coll.insert(_script("ezgd-cosmetic-site", filterlists.style_js(selectors, "site"),
                                QWebEngineScript.ApplicationWorld, False))

    def _cosmetic(self, page, payload: str) -> None:
        """The page reported its classes/ids: hide the ones generic filter rules name as ads."""
        import json
        if not (ADBLOCK.enabled and page.cosm_generic):
            return
        try:
            data = json.loads(payload)
        except ValueError:
            return
        sel = [x for x in filterlists.generic_selectors(list(data.get("c") or []), list(data.get("i") or []),
                                                       page.cosm_exceptions) if x not in page.cosm_sent]
        if sel:
            page.cosm_sent.update(sel)
            page.runJavaScript(filterlists.style_js(sel, "generic"), QWebEngineScript.ApplicationWorld)

    # ------------------------------------------------ page trouble
    def _page_message(self, page, message: str, asked: bool = False) -> None:
        text = " ".join(str(message or "").split())[:240]
        if not text:
            return
        self._show_notice(("The page asked: " if asked else "The page says: ") + f"“{text}”" +
                          (" — answered No (pages cannot open message boxes here)." if asked else ""))

    def _close_tab(self, page) -> None:
        """window.close() from a page: drop that tab (the first page stays)."""
        if page in self._pending:
            self._pending.remove(page)
            page.deleteLater()
            return
        if page in self.tabs and len(self.tabs) > 1:
            showing = self.current is page
            self.tabs.remove(page)
            if showing:
                self._switch_to(self.tabs[-1])
            self._drop(page)

    def _page_crashed(self, page, status) -> None:
        if status == QWebEnginePage.NormalTerminationStatus or page is not self.current:
            return
        self._show_notice("The page crashed.", "Reload", lambda: self._replace_current(True))

    def _replace_current(self, reload: bool) -> None:
        """Swap the current tab for a fresh page (a hung or crashed page cannot be revived in place)."""
        old = self.current
        url = old.url()
        tab = _Page(profile(), self)
        _PAGES.add(tab)
        idx = self.tabs.index(old) if old in self.tabs else -1
        if idx >= 0:
            self.tabs[idx] = tab
        else:
            self.tabs.append(tab)
        self._switch_to(tab)
        self._drop(old)
        self._ping_sent = 0.0
        self.notice.hide()
        if reload and url.isValid() and not url.isEmpty():
            tab.load(url)
        else:
            tab.setHtml("<html><body style='font-family:sans-serif;background:#10131b;color:#c9cfdd;padding:40px'>"
                        "<h2>Page stopped</h2><p>The page stopped responding and was closed. Press Back or paste a "
                        "link above.</p></body></html>")

    def _page_loading(self, page) -> None:
        """A new document drops any answer still pending from the old one: start the hang check afresh."""
        if page is self._ping_page:
            self._ping_sent = 0.0
            if self._hang_shown:
                self._hang_shown = False
                self.notice.hide()

    def _watchdog(self) -> None:
        """Every few seconds: is the page's script still answering? If not, offer to stop it — and when it stays
        frozen, stop it by itself (a hung page also ignores clicks, so the window looked frozen)."""
        if not self.isVisible():
            return
        now = time.monotonic()
        page = self.current
        if self._ping_sent and self._ping_page is page and now - self._ping_sent > HANG_SECONDS:
            if not self._hang_shown:
                self._hang_shown = True
                log.warning("browser: %s is not responding", page.url().toString()[:200])
                self._show_notice("This page is not responding.", "Stop this page",
                                  lambda: self._replace_current(False))
                self.notice.add_action("Reload", lambda: self._replace_current(True))
            elif now - self._ping_sent > HANG_STOP_SECONDS:
                log.warning("browser: stopped %s (no answer for %.0f s)", page.url().toString()[:200],
                            now - self._ping_sent)
                self._replace_current(False)
                self._show_notice("The page stopped responding, so it was closed.", "Reload",
                                  lambda: self._replace_current(True))
            return
        if self._ping_sent and self._ping_page is page:
            return                                  # still waiting for the last answer

        def pong(_r, page=page) -> None:
            import shiboken6
            if not shiboken6.isValid(self):
                return                              # an answer that arrived after the window was closed
            if page is self._ping_page:
                self._ping_sent = 0.0
                if self._hang_shown:
                    self._hang_shown = False
                    self.notice.hide()
        self._ping_page = page
        self._ping_sent = now
        try:
            page.runJavaScript("1", QWebEngineScript.ApplicationWorld, pong)
        except RuntimeError:
            self._ping_sent = 0.0

    def _update_shield(self) -> None:
        if not self.isVisible():
            return
        n = ADBLOCK.count(_site(_host(self.current.url()))) + self._popups_blocked
        self.shield_count.setText(f"{n} blocked" if ADBLOCK.enabled and n else "")

    def _toggle_adblock(self, on: bool) -> None:
        ADBLOCK.enabled = on
        self.actions.settings["browser_adblock"] = on
        try:
            self.actions.bridge.call(self.actions.core.set_setting("browser_adblock", on))
        except Exception:          # settings are a convenience here
            pass
        self.current.triggerAction(QWebEnginePage.Reload)

    def _open(self, url: QUrl) -> None:
        self.notice.hide()
        self.current.load(url)

    def back(self) -> None:
        if self.current.history().canGoBack():
            self.current.triggerAction(QWebEnginePage.Back)
        elif len(self.tabs) > 1 and self.current is self.tabs[-1]:
            self._closed.append(self.tabs.pop())      # back to the page the user clicked from
            self._switch_to(self.tabs[-1])

    def _go(self) -> None:
        url = QUrl.fromUserInput(self.address.text().strip())
        if url.scheme() in ("http", "https"):
            self.current.load(url)

    def _external(self) -> None:
        from ezgd.ui.dialogs.misc import open_url
        open_url(self.current.url().toString() or self.start_url)
        self.actions.toasts.show("Opened in your browser", "When the file has downloaded, use “Import downloaded "
                                 "file…” on the game page.", "info")

    def _show_notice(self, text: str, action: str | None = None, fn=None) -> None:
        self.notice.set_text(text, "warning")
        self.notice.clear_actions()
        if action and fn:
            self.notice.add_action(action, fn)
        self.notice.show()

    def blocked_download(self, name: str) -> None:
        self._show_notice(f"Blocked the download of “{name}”: it is a program, not a game archive — on file hosts "
                          "these are almost always ads. Look for the real download button.")

    # ------------------------------------------------------------- transfers
    def download_started(self, d: BrowserDownload) -> None:
        self.started.append(d)
        if self.isVisible():
            from ezgd.installation.volumes import volume_info
            v = volume_info(d.name)
            if v is not None:
                self.actions.toasts.show(f"Part {v.index} started", "Get the other parts from the game page too — "
                                         "the game is extracted once the last part is in.", "success")
            else:
                self.actions.toasts.show("Download started", f"{d.name} — see the Downloads page.", "success")
            # hidden, not destroyed: the page stays alive until its downloads are done
            QTimer.singleShot(600, self.hide)

    def downloads_settled(self) -> None:
        if not self.isVisible():
            self._dispose()

    def _dispose(self) -> None:
        import shiboken6
        self._watch.stop()
        self._shield_timer.stop()
        for tab in self.tabs + self._pending + self._closed:
            if shiboken6.isValid(tab):
                tab.deleteLater()
        self._pending, self._closed = [], []
        self.deleteLater()

    def hideEvent(self, ev) -> None:
        super().hideEvent(ev)
        self.went_away.emit()

    def couch_mode(self, zoom: float) -> None:
        """Opened from couch mode: no desktop toolbar (the controller does all of it), pages zoomed for the TV."""
        self._banner.hide()
        self._bar_widget.hide()
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.zoom = zoom
        for v in self._views.values():
            v.setZoomFactor(zoom)

    def closeEvent(self, ev) -> None:
        if any(d.state in ("downloading", "paused") for d in self.started):
            self.hide()                              # downloads keep running; disposed when they finish
            ev.ignore()
            return
        # not QDialog.closeEvent: it turns the close into reject(), whose close() inside a close is dropped — so
        # Alt+F4 / the title bar's ✕ were refused and the window stayed (a friend was locked in it)
        ev.accept()
        self.hide()
        self.went_away.emit()
        self._dispose()

    def reject(self) -> None:          # Esc behaves like Cancel
        self.close()


def active_count() -> int:
    """Running in-app browser transfers (for the quit prompt)."""
    if not AVAILABLE or _MANAGER is None:
        return 0
    import shiboken6
    return _MANAGER.active() if shiboken6.isValid(_MANAGER) else 0


def shutdown() -> None:
    """Stop transfers and delete every page before the shared profile (Qt WebEngine aborts otherwise)."""
    global _PROFILE, _MANAGER, _INTERCEPTOR
    if not AVAILABLE or _PROFILE is None:
        return
    import shiboken6
    if _MANAGER is not None:
        _MANAGER.cancel_all()
        _MANAGER = None
    for page in list(_PAGES or ()):
        if shiboken6.isValid(page):
            view = QWebEngineView.forPage(page)
            if view is not None and shiboken6.isValid(view):
                view.setPage(None)
            shiboken6.delete(page)
    if shiboken6.isValid(_PROFILE):
        _PROFILE.setUrlRequestInterceptor(None)       # detach before anything is freed (IO thread may be in it)
        shiboken6.delete(_PROFILE)
    _PROFILE = None
    _INTERCEPTOR = None


def open_browser_download(actions, game, url: str, host_label: str, parent):
    if not AVAILABLE:
        QMessageBox.information(parent, "In-app browser not installed",
                                "The in-app browser component (Qt WebEngine) is not part of this build. Use “Open "
                                "in browser” and then “Import downloaded file…”.")
        return None
    # owned by the main window, not the game page: closing the game page must not stop the download
    dlg = BrowserDownloadDialog(actions, game, url, host_label, actions.parent() or parent)
    dlg.setAttribute(Qt.WA_DeleteOnClose, False)     # disposed explicitly once its downloads are done
    dlg.show()
    return dlg
