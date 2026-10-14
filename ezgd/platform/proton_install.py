"""Install GE-Proton on demand (official GitHub releases), so the debugging assistant can test it.

A different Proton build is the most common fix for a game that does not run. GE-Proton is published on
https://github.com/GloriousEggroll/proton-ge-custom/releases with a ``.sha512sum`` next to every tarball; the
download is verified against it and extracted (symlinks inside the archive are kept, anything pointing outside
the target folder is refused) into Steam's ``compatibilitytools.d`` — where Steam, umu-launcher and this app
all find it.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

RELEASES = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"


@dataclass
class GERelease:
    tag: str
    tar_url: str
    tar_name: str
    sha_url: str
    size: int


class ProtonInstallError(Exception):
    pass


def parse_release(body: bytes) -> GERelease:
    try:
        d = json.loads(body)
    except ValueError as exc:
        raise ProtonInstallError("GitHub returned an unexpected answer") from exc
    tag = d.get("tag_name") or ""
    assets = {a.get("name", ""): a for a in d.get("assets") or [] if isinstance(a, dict)}
    tar = next((a for n, a in assets.items() if n.endswith(".tar.gz") and "aarch64" not in n), None)
    sha = next((a for n, a in assets.items() if n.endswith(".sha512sum") and "aarch64" not in n), None)
    if not tag or tar is None or sha is None:
        raise ProtonInstallError("The latest GE-Proton release has no x86_64 download")
    return GERelease(tag, tar["browser_download_url"], tar["name"], sha["browser_download_url"],
                     int(tar.get("size") or 0))


def parse_sha512(text: str, name: str) -> str:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*").endswith(name) and re.fullmatch(r"[0-9a-f]{128}", parts[0]):
            return parts[0]
    m = re.search(r"\b[0-9a-f]{128}\b", text)
    if not m:
        raise ProtonInstallError("The release's SHA-512 checksum file could not be read")
    return m.group(0)


def compat_dir(steam_root: Path | None) -> Path:
    root = steam_root or Path.home() / ".local" / "share" / "Steam"
    return root / "compatibilitytools.d"


def installed(steam_root: Path | None) -> list[str]:
    d = compat_dir(steam_root)
    return sorted(p.name for p in d.iterdir() if (p / "proton").is_file()) if d.is_dir() else []


def belongs_to(name: str, tag: str) -> bool:
    """``GE-Proton11-7-x86_64`` (folder in releases since 11-x) and ``GE-Proton11-7`` are both release 11-7."""
    return name == tag or (name.startswith(tag + "-") and not name[len(tag) + 1:][:1].isdigit())


def is_installed(steam_root: Path | None, tag: str) -> bool:
    return any(belongs_to(n, tag) for n in installed(steam_root))


def ge_version(name: str) -> tuple[int, int] | None:
    m = re.search(r"GE-Proton(\d+)-(\d+)", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def extract(tarball: Path, steam_root: Path | None, tag: str) -> Path:
    """Extract a GE-Proton tarball into compatibilitytools.d; returns the new tool folder.

    The archive must hold exactly one folder, named after the release (``GE-Proton11-7`` or, since GE-Proton 11,
    ``GE-Proton11-7-x86_64``)."""
    dest = compat_dir(steam_root)
    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / f".ezgd-installing-{tag}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            tops = {m.name.lstrip("./").split("/", 1)[0] for m in tf.getmembers()} - {""}
            if len(tops) != 1 or not belongs_to(next(iter(tops)), tag):
                raise ProtonInstallError(f"Unexpected archive layout: expected one {tag} folder, found "
                                         f"{', '.join(sorted(tops))[:80]}")
            top = next(iter(tops))
            tf.extractall(staging, filter="data")      # links inside the archive kept; anything escaping refused
        tool = staging / top
        if not (tool / "proton").is_file() or not (tool / "compatibilitytool.vdf").is_file():
            raise ProtonInstallError("The download does not contain a Proton build")
        final = dest / top
        if final.exists():
            shutil.rmtree(final)
        tool.rename(final)
        return final
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ProtonInstallError(f"Could not unpack GE-Proton: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
