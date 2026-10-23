"""System facts that make games fail in ways the game's own logs do not explain (Linux).

Read-only: values are read from /proc, the filesystem and (if installed) ``vulkaninfo``; nothing is changed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
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
    vulkaninfo: bool = False                 # vulkaninfo was available (GPU list is meaningful)


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


_gpus_lock = threading.Lock()
_gpus_found: tuple[list[GPU], bool] | None = None


def gpus() -> tuple[list[GPU], bool]:
    """The GPUs as Vulkan sees them — ``vulkaninfo`` runs once per run of the app, and never twice at once.

    It ran for every Diagnose (the Library asks for several games at once): bursts of up to six ``vulkaninfo`` within a
    second made the NVIDIA driver hold KWin's page flips — the whole screen froze for 1–14 s ("Pageflip timed out",
    RTX 4090, driver 615.78, 2026-10-23). Without them, none in the same runs. GPUs do not change while the app runs."""
    global _gpus_found
    with _gpus_lock:
        if _gpus_found is None:
            _gpus_found = _read_gpus()
        return list(_gpus_found[0]), _gpus_found[1]


def _read_gpus() -> tuple[list[GPU], bool]:
    if os.environ.get("EZGD_VULKANINFO", "1") == "0":
        return [], False                         # (tests: never the real GPU driver)
    exe = shutil.which("vulkaninfo")
    if not exe:
        return [], False
    try:
        out = subprocess.run([exe, "--summary"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return [], False
    found = []
    for block in re.split(r"\nGPU\d+:", out)[1:]:
        name = re.search(r"deviceName\s*=\s*(.+)", block)
        drv = re.search(r"driverInfo\s*=\s*(.+)", block) or re.search(r"driverName\s*=\s*(.+)", block)
        api = re.search(r"apiVersion\s*=\s*(\S+)", block)
        if name:
            found.append(GPU(name.group(1).strip(), drv.group(1).strip() if drv else "",
                             api.group(1).strip() if api else ""))
    return found, True


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
