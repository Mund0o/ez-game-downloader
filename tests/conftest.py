"""Shared fixtures: temp app dirs, databases, a scriptable local HTTP server, fixture loaders."""

from __future__ import annotations

import hashlib
import http.server
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

# no test may ever see (or change) this computer's real Steam: 2026-10-12 a test found the user's real Steam game
# and unpacked a dummy fix into it. Tests that need Steam build a fake one and point "steam_root" at it.
os.environ["EZGD_STEAM_ROOT"] = "/nonexistent/ezgd-test-steam"
# nor talk to it: 2026-10-20 a quick-menu test reached the real Steam client's debugging port (our overlay's bridge)
# and listed the user's real chats. Port 1: nothing listens; bridge tests start a fake Steam on a port of their own.
os.environ["EZGD_STEAM_BRIDGE_PORT"] = "1"
os.environ["EZGD_NO_GAMEPADS"] = "1"       # never the real controllers (a game played meanwhile pressed buttons in tests)
os.environ["EZGD_QUICK_KEY"] = "0"          # never a Shift+Tab shortcut in the real desktop (faked where tested)
os.environ["EZGD_HIDDEN"] = "0"             # never KWin scripts in the real desktop (hidden tests): faked where tested
os.environ["EZGD_AWAKE"] = "0"              # never a real idle hold on the desktop: a fake systemd-inhibit where tested
os.environ["EZGD_STEAM_INPUT"] = "0"        # never steam://forceinputappid to the real Steam client (faked where tested)
os.environ["EZGD_VULKANINFO"] = "0"          # not the real machine's graphics cards (kernel lists)
os.environ["EZGD_SYSLOG"] = "0"             # never the real system journal (recorded journal entries where tested)
os.environ["EZGD_CROWD"] = "0"               # never the real knowledge server (a fake one where tested)
os.environ["EZGD_NO_INTRO"] = "1"          # couch mode opens straight away (tests of the intro switch it back on)
os.environ["EZGD_NO_SOUNDS"] = "1"         # couch mode's sounds are only counted, never played through the speakers

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:                    # Qt WebEngine (in-app browser) must be loaded before the QApplication exists
    import PySide6.QtWebEngineWidgets  # noqa: F401
except ImportError:
    pass

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def app_paths(tmp_path):
    from ezgd.platform.paths import resolve_paths
    p = resolve_paths(tmp_path / "home")
    p.ensure()
    return p


@pytest.fixture
def db(tmp_path):
    from ezgd.database.db import Database
    d = Database(tmp_path / "test.sqlite3", backup_dir=tmp_path / "backups").open()
    yield d
    d.close()


@pytest.fixture
def settings(db, tmp_path):
    from ezgd.core.settings import SettingsStore
    s = SettingsStore(db)
    s.set("download_dir", str(tmp_path / "downloads"))
    s.set("install_dir", str(tmp_path / "games"))
    s.set("steam_overlay", False)                  # never load this computer's real Steam overlay into test games
    return s


# ------------------------------------------------------------- HTTP server
@dataclass
class Resource:
    data: bytes = b""
    status: int = 200
    content_type: str = "application/octet-stream"
    etag: str | None = None
    support_range: bool = True
    drop_after: int | None = None          # close connection after N body bytes …
    drop_times: int = 0                     # … for this many requests
    status_sequence: list[int] = field(default_factory=list)   # e.g. [503, 503] before normal
    redirect_to: str | None = None
    redirect_status: int = 302
    chunk_delay: float = 0.0
    headers: dict = field(default_factory=dict)
    hits: int = 0
    range_requests: list[str] = field(default_factory=list)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head: bool):
        res: Resource | None = self.server.resources.get(self.path.split("?")[0])
        if res is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        res.hits += 1
        if res.status_sequence:
            code = res.status_sequence.pop(0)
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if res.redirect_to:
            self.send_response(res.redirect_status)
            self.send_header("Location", res.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = res.data
        start, status = 0, res.status
        rng = self.headers.get("Range")
        if rng:
            res.range_requests.append(rng)
        if_range = self.headers.get("If-Range")
        if rng and res.support_range and (not if_range or if_range == res.etag):
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        body = data[start:]
        self.send_response(status)
        self.send_header("Content-Type", res.content_type)
        self.send_header("Content-Length", str(len(body)))
        if res.support_range:
            self.send_header("Accept-Ranges", "bytes")
        if res.etag:
            self.send_header("ETag", res.etag)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        for k, v in res.headers.items():
            self.send_header(k, v)
        self.end_headers()
        if head:
            return
        limit = None
        if res.drop_after is not None and res.drop_times > 0:
            res.drop_times -= 1
            limit = res.drop_after
        sent = 0
        step = 16 * 1024
        try:
            while sent < len(body):
                chunk = body[sent:sent + step]
                if limit is not None and sent + len(chunk) > limit:
                    chunk = chunk[: max(0, limit - sent)]
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.wfile.write(chunk)
                sent += len(chunk)
                if res.chunk_delay:
                    time.sleep(res.chunk_delay)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class LocalServer:
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.resources = {}
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def add(self, path: str, **kw) -> Resource:
        r = Resource(**kw)
        self.httpd.resources[path] = r
        return r

    def url(self, path: str) -> str:
        return self.base + path

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = LocalServer()
    yield s
    s.close()


@pytest.fixture
async def local_client():
    from ezgd.net.client import SafeHttpClient
    c = SafeHttpClient(allow_private=True, timeout=10)
    yield c
    await c.aclose()


def random_bytes(n: int, seed: int = 1) -> bytes:
    out = bytearray()
    h = hashlib.sha256(str(seed).encode()).digest()
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out.extend(h)
    return bytes(out[:n])


# --------------------------------------------------------- mock transport
class Routes:
    """httpx.MockTransport router keyed by full URL (query included) or prefix match."""

    def __init__(self):
        self.routes: dict[str, httpx.Response | callable] = {}
        self.calls: list[httpx.Request] = []

    def add(self, url: str, body: bytes | str = b"", status: int = 200, headers: dict | None = None,
            content_type: str = "text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        hdrs = {"content-type": content_type, **(headers or {})}
        self.routes[url] = lambda req: httpx.Response(status, content=body, headers=hdrs)

    def add_handler(self, url: str, fn):
        self.routes[url] = fn

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url in self.routes:
            return self.routes[url](request)
        for k, fn in self.routes.items():
            if k.endswith("*") and url.startswith(k[:-1]):
                return fn(request)
        return httpx.Response(404, content=b"not found", headers={"content-type": "text/plain"})


@pytest.fixture
def routes():
    return Routes()


@pytest.fixture
async def mock_fetcher(routes, db):
    from ezgd.database.repositories import HttpCacheRepository
    from ezgd.net.client import SafeHttpClient
    from ezgd.sources.cache import CachedFetcher
    client = SafeHttpClient(transport=httpx.MockTransport(routes))
    yield CachedFetcher(client, HttpCacheRepository(db), default_ttl=3600)
    await client.aclose()


def make_torrent(files: dict[str, bytes], root: Path, name: str = "payload", piece_length: int = 16384) -> bytes:
    """Create a real .torrent for files written under root/name; returns bencoded bytes."""
    import libtorrent as lt
    base = root / name
    for rel, data in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    fs = lt.file_storage()
    lt.add_files(fs, str(base))
    ct = lt.create_torrent(fs, piece_length)
    lt.set_piece_hashes(ct, str(root))
    return lt.bencode(ct.generate())


@pytest.fixture(scope="session", autouse=True)
def _browser_shutdown():
    yield
    try:
        from ezgd.ui.dialogs import browser
        browser.shutdown()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _main_thread_gc():
    """Same rule as the app: no automatic collection on worker threads; collect on the main thread instead."""
    import gc
    gc.disable()
    yield
    gc.collect()
