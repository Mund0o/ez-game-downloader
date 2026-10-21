"""Reading and writing files in a folder other people can write to (the synced save folder).

Syncthing and other sync tools carry symbolic links too: someone with access to the folder could put a link where
the app writes (``.part`` names, the backup's own name) and have it overwrite a file of the user's elsewhere, or
where it reads, to have it read one. So: nothing is followed through a link, writes go to a fresh random name first,
and reads are size-limited.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

MAX_READ = 4 * 1024 * 1024


def read_file(path: Path) -> bytes | None:
    """A regular file of sane size — never through a link (the synced folder is not trusted)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_READ:
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read(MAX_READ + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def write_file(folder: Path, name: str, blob: bytes | Path) -> Path:
    """Into ``folder`` (not itself a link) under a fresh random temporary name, then renamed over ``name`` — a link
    planted at either name is replaced, never followed."""
    if folder.is_symlink():
        raise OSError(f"{folder} is a link — not writing into it")
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            if isinstance(blob, Path):                 # a file, streamed (save backups can be large)
                import shutil
                with open(blob, "rb") as src:
                    shutil.copyfileobj(src, fh, 1024 * 1024)
            else:
                fh.write(blob)
        os.replace(tmp, folder / name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return folder / name
