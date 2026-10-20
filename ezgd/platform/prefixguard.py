"""Which Proton builds can share a Wine prefix.

Proton upgrades a prefix made by an older build of its own kind. Between kinds it cannot: GE-Proton and
Proton-CachyOS name their prefix versions "GE-Proton11-7" / "CachyOS-11.0-100", and their upgrade code splits those
on "-" expecting two parts — it fails ("Prefix has an invalid version?!") and then runs the game in a prefix set up by
a different Wine. Seen 2026-10-13: Slay the Spire 2 under GE-Proton11-7 in a prefix made by Proton 11.0 ("11.0-100").
(Its OnlineFix "Failed to load original steamclient. Error code: 998" turned out to have another cause: GE-Proton11-7
switches Wine's Steam client bridge off for UMU_ID launches — see proton.umu_steam_mode.) A downgrade (a prefix from
a newer Proton) breaks the same way. So each family of Proton builds gets its own prefix for a game, and saves move
along.
"""

from __future__ import annotations

import re
from pathlib import Path

_CACHE: dict[str, tuple[float, str | None]] = {}


def proton_prefix_version(proton_dir: Path | str | None) -> str | None:
    """CURRENT_PREFIX_VERSION from a Proton build's ``proton`` script (what it writes into a prefix)."""
    if not proton_dir:
        return None
    script = Path(proton_dir) / "proton"
    try:
        mtime = script.stat().st_mtime
    except OSError:
        return None
    hit = _CACHE.get(str(script))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        m = re.search(r'^CURRENT_PREFIX_VERSION\s*=\s*"([^"]+)"', script.read_text("utf-8", "replace"), re.M)
    except OSError:
        m = None
    v = m.group(1) if m else None
    _CACHE[str(script)] = (mtime, v)
    return v


def prefix_version(prefix: Path) -> str | None:
    try:
        return (Path(prefix) / "version").read_text("utf-8", "replace").strip() or None
    except OSError:
        return None


def family(version: str | None) -> str | None:
    if not version:
        return None
    if version.startswith("GE-Proton"):
        return "ge"
    if version.lower().startswith("cachyos"):
        return "cachyos"
    if re.match(r"^\d+\.\d+-\d+$", version):
        return "valve"
    return "other-" + re.sub(r"[^A-Za-z]", "", version)[:12].lower()


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version))


def compatible(prefix_ver: str | None, proton_ver: str | None) -> bool:
    """Can a build writing ``proton_ver`` run in a prefix last set up as ``prefix_ver``?"""
    if not prefix_ver or not proton_ver or prefix_ver == proton_ver:
        return True
    if family(prefix_ver) != family(proton_ver):
        return False
    return _numbers(proton_ver) >= _numbers(prefix_ver)          # same kind: an upgrade is fine, a downgrade not


def slot_name(base: str, fam: str) -> str:
    return f"{base}.proton-{fam}"


MARKER = ".ezgd-proton"          # the Proton kind that set this prefix up (written by the app)
_UPGRADE = re.compile(r"Upgrading prefix from (\S+) to (\S+) \((.+?)/?\)\s*$")


def failed_upgrade_origin(log_text: str, prefix: Path) -> str | None:
    """The version a prefix really had before a Proton of another kind failed to upgrade it and stamped its own
    version on it anyway ("Upgrading prefix from 11.0-100 to GE-Proton11-7 (…)" + "invalid version"). Only log lines
    after the app last switched this game's prefix count."""
    text = log_text.rsplit("=== prefix switched", 1)[-1]
    lines = text.splitlines()
    want = str(Path(prefix)).rstrip("/")
    for i, ln in enumerate(lines):
        m = _UPGRADE.search(ln)
        if not m or m.group(3).rstrip("/") != want:
            continue
        if any("invalid version" in x for x in lines[i + 1:i + 4]) and family(m.group(1)) != family(m.group(2)):
            return m.group(1)                        # the earliest failed hop: what made the prefix
    return None


def effective_version(prefix: Path, log_text: str = "") -> tuple[str | None, bool]:
    """(what really set this prefix up, damaged by a failed cross-kind upgrade?)."""
    try:
        marked = (Path(prefix) / MARKER).read_text("utf-8", "replace").strip()
    except OSError:
        marked = ""
    if marked:
        return marked, False
    origin = failed_upgrade_origin(log_text, prefix) if log_text else None
    if origin:
        return origin, True
    return prefix_version(prefix), False


def mark(prefix: Path, version: str | None) -> None:
    if not version:
        return
    try:
        Path(prefix).mkdir(parents=True, exist_ok=True)
        (Path(prefix) / MARKER).write_text(version + "\n", "utf-8")
    except OSError:
        pass
