"""System facts that make games fail in ways the game's own logs do not explain (Linux).

Read-only: values are read from /proc, /sys and the filesystem; nothing is changed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

MAP_COUNT_RECOMMENDED = 1_048_576       # SteamOS / Fedora / Arch now ship 2147483642 or 1048576
NOFILE_FOR_ESYNC = 524_288
FRAGILE_FS = {"ntfs": "NTFS", "ntfs3": "NTFS", "fuseblk": "NTFS/exFAT (FUSE)", "exfat": "exFAT", "vfat": "FAT32",
              "msdos": "FAT", "fat": "FAT"}


@dataclass
class GPU:
    name: str
    driver: str = ""
    api: str = ""


@dataclass
class SystemFacts:
    max_map_count: int | None = None
    nofile_hard: int | None = None
    free_game: int | None = None             # bytes free on the game's drive
    free_prefix: int | None = None
    fs_game: str | None = None               # filesystem type under the game folder
    fs_prefix: str | None = None
    gpus: list[GPU] = field(default_factory=list)
    vulkaninfo: bool = False                 # the GPU list is meaningful (the name is from when vulkaninfo read it)


def _read_int(p: str) -> int | None:
    try:
        return int(Path(p).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def fs_type(path: Path) -> str | None:
    """Filesystem type of the mount that contains ``path`` (longest matching mount point)."""
    try:
        real = os.path.realpath(path)
        best, kind = "", None
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            mnt = parts[1].replace("\\040", " ")
            if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                best, kind = mnt, parts[2]
        return kind
    except OSError:
        return None


def _free(path: Path) -> int | None:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def gpus() -> tuple[list[GPU], bool]:
    """The graphics cards, as the kernel lists them (ezgd.platform.machine) — never by starting a GPU tool.

    This used to run ``vulkaninfo``, which starts every Vulkan driver: in bursts (Diagnose for several games at once) the
    NVIDIA driver held KWin's page flips and the whole screen froze for 1–14 s; even once — the Library's "before you
    play" check at every start — KWin stalled ~80 ms five times, so the whole desktop stuttered while the app opened
    (RTX 4090, driver 615.78, 2026-10-23). Diagnose only shows the names."""
    if os.environ.get("EZGD_VULKANINFO", "1") == "0":
        return [], False                         # (tests: not the real machine's cards)
    from ezgd.platform.machine import _gpus
    found = [GPU(g["name"], g["driver"]) for g in _gpus()]
    return found, bool(found)


def collect(game_dir: Path, prefix_dir: Path, with_gpus: bool = True) -> SystemFacts:
    f = SystemFacts()
    f.max_map_count = _read_int("/proc/sys/vm/max_map_count")
    try:
        import resource
        f.nofile_hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    except (ImportError, ValueError, OSError):
        f.nofile_hard = None
    f.free_game, f.free_prefix = _free(game_dir), _free(prefix_dir)
    f.fs_game, f.fs_prefix = fs_type(game_dir), fs_type(prefix_dir if prefix_dir.exists() else prefix_dir.parent)
    if with_gpus:
        f.gpus, f.vulkaninfo = gpus()
    return f
