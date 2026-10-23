"""The computer a failure happened on — what makes the same-looking error a different problem elsewhere.

The same crash can have different causes on different computers: a crash in nvapi64.dll means something on an
NVIDIA card and nothing on AMD; "illegal instruction" depends on the CPU's instruction sets; a freeze on an NTFS
drive is a filesystem problem; a driver version can break one game on one PC. Every solved failure (ezgd.library
.tracer's memory) and every crash record keeps this profile, and a match from a similar computer counts for more.

Read from files and quick commands only (no network, nothing personal: the machine id is hashed).
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def _read(p: str) -> str:
    try:
        return Path(p).read_text("utf-8", "replace")
    except OSError:
        return ""


def _os_release() -> dict:
    out = {}
    for ln in _read("/etc/os-release").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k] = v.strip().strip('"')
    return out


def _cpu() -> dict:
    info = _read("/proc/cpuinfo")
    model = re.search(r"^model name\s*:\s*(.+)$", info, re.M)
    flags = re.search(r"^flags\s*:\s*(.+)$", info, re.M)
    have = set((flags.group(1) if flags else "").split())
    wanted = ("sse4_1", "sse4_2", "avx", "avx2", "fma", "bmi2", "avx512f", "f16c", "popcnt", "sha_ni")
    vendor = re.search(r"^vendor_id\s*:\s*(\S+)", info, re.M)
    return {"model": (model.group(1).strip() if model else platform.processor() or "")[:80],
            "vendor": {"GenuineIntel": "intel", "AuthenticAMD": "amd"}.get(vendor.group(1) if vendor else "", "other"),
            "cores": os.cpu_count() or 0, "flags": [f for f in wanted if f in have]}


def _gpus() -> list[dict]:
    """The graphics cards as the kernel sees them. (Not vulkaninfo: the profile is read at every start, and starting
    every Vulkan driver made the whole desktop stutter while the app opened.)"""
    out = []
    nv_version = re.search(r"Kernel Module.*?\s(\d+\.\d+(?:\.\d+)?)\s", _read("/proc/driver/nvidia/version"))
    for d in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        v = _read(str(d / "device" / "vendor")).strip()
        vendor = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}.get(v)
        if not vendor:
            continue
        drv = Path(os.path.realpath(d / "device" / "driver")).name
        slot = Path(os.path.realpath(d / "device")).name
        model = re.search(r"^Model:\s*(.+)$", _read(f"/proc/driver/nvidia/gpus/{slot}/information"), re.M)
        name = model.group(1).strip() if model else vendor.upper() + " GPU"
        if vendor == "nvidia" and nv_version:
            drv = f"{drv} {nv_version.group(1)}"
        out.append({"name": name[:80], "driver": drv[:60], "vendor": vendor})
    return out


def _ram_gb() -> int:
    m = re.search(r"MemTotal:\s+(\d+)", _read("/proc/meminfo"))
    return round(int(m.group(1)) / 1024 / 1024) if m else 0


def _audio() -> str:
    if shutil.which("pactl"):
        try:
            out = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"Server Name:\s*(.+)", out)
            if m:
                return "pipewire" if "PipeWire" in m.group(1) else m.group(1).strip()[:40]
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


@lru_cache(maxsize=1)
def profile() -> dict:
    """This computer, in the terms that decide whether a fix carries over."""
    osr = _os_release()
    mid = _read("/etc/machine-id").strip()
    return {
        "id": hashlib.sha256(("ezgd:" + mid).encode()).hexdigest()[:16] if mid else "",
        "os": osr.get("PRETTY_NAME") or platform.system(),
        "distro": osr.get("ID", ""),
        "kernel": platform.release(),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "cpu": _cpu(),
        "gpus": _gpus(),
        "ram_gb": _ram_gb(),
        "max_map_count": int(_read("/proc/sys/vm/max_map_count").strip() or 0),
        "audio": _audio(),
    }


def gpu_vendor(p: dict) -> str:
    """The vendor of the GPU games run on (a discrete card over an integrated one)."""
    vendors = [g.get("vendor") for g in p.get("gpus") or []]
    for v in ("nvidia", "amd", "intel"):
        if v in vendors:
            return v
    return vendors[0] if vendors else ""


def similarity(a: dict, b: dict) -> tuple[float, list[str]]:
    """How alike two computers are for a fix to carry over (0–1), and the differences that matter."""
    if not a or not b:
        return 0.8, []
    if a.get("id") and a.get("id") == b.get("id"):
        return 1.0, []
    diff, score = [], 1.0
    va, vb = gpu_vendor(a), gpu_vendor(b)
    if va and vb and va != vb:
        diff.append(f"{va.upper()} vs {vb.upper()} graphics")
        score -= 0.25
    fa, fb = set((a.get("cpu") or {}).get("flags") or []), set((b.get("cpu") or {}).get("flags") or [])
    if fa != fb and ({"avx2", "avx512f"} & (fa ^ fb)):
        diff.append("different CPU instruction sets (" + ", ".join(sorted({"avx2", "avx512f"} & (fa ^ fb))) + ")")
        score -= 0.1
    if (a.get("session") or "") != (b.get("session") or ""):
        diff.append(f"{a.get('session') or '?'} vs {b.get('session') or '?'} session")
        score -= 0.05
    if (a.get("distro") or "") != (b.get("distro") or ""):
        score -= 0.03
    return max(0.3, score), diff
