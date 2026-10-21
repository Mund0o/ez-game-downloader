"""One running app per data folder.

A second copy would open the same database, start a second torrent engine on the same port and resume the same
downloads into the same files. So the first copy holds an exclusive lock on ``<data>/instance.lock`` for as long as
it runs (the operating system releases it even if the app crashes), and a second start just asks the running copy
to show its window and exits.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_HELD: list = []                     # the open lock file (kept for the life of the process)


def server_name(data_dir: Path) -> str:
    return "ezgd-" + hashlib.sha1(str(Path(data_dir).resolve()).encode()).hexdigest()[:12]


def acquire(data_dir: Path) -> bool:
    """True when this process is now the only running app for ``data_dir``."""
    path = Path(data_dir) / "instance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()).encode())
    fh.flush()
    _HELD.append(fh)
    return True


def release() -> None:
    while _HELD:
        fh = _HELD.pop()
        try:
            fh.close()
        except OSError:
            pass


def ask_running_copy_to_show(data_dir: Path, timeout_ms: int = 1500) -> bool:
    """Tell the running app to bring its window to the front (needs a QCoreApplication)."""
    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(server_name(data_dir))
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show\n")
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


def listen(data_dir: Path, on_show, parent=None):
    """In the running app: answer later starts by showing the window. Returns the server (keep a reference)."""
    from PySide6.QtNetwork import QLocalServer
    name = server_name(data_dir)
    QLocalServer.removeServer(name)               # left behind by a crash; we hold the lock, so it is ours
    server = QLocalServer(parent)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)    # only this user may connect
    if not server.listen(name):
        log.warning("could not listen for other app starts: %s", server.errorString())
        return server

    def accept() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: (c.readAll(), on_show()))
            conn.disconnected.connect(conn.deleteLater)
    server.newConnection.connect(accept)
    return server
