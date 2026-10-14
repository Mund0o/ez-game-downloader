"""How Steam itself runs a game — the best reference when the same game works when Steam starts it.

For games that are also Steam games (fix-only OnlineFix downloads that go into a Steam game, or any game whose Steam
App ID is known and that is installed in Steam), Steam keeps its own prefix in ``steamapps/compatdata/<appid>``. Its
``config_info`` names the Proton build that last ran it, and its ``system32`` holds the redistributables Steam
installed on the first start (Visual C++, DirectX, .NET …). Comparing that with the app's prefix shows what is
missing. Everything here only reads Steam's files.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _compatdata(steam_root: Path, app_id: int) -> Path | None:
    from ezgd.platform import steam
    try:
        libs = steam.library_folders(steam_root)
    except Exception:
        libs = [steam_root]
    for lib in [steam_root, *libs]:
        p = Path(lib) / "steamapps" / "compatdata" / str(app_id)
        if (p / "pfx").is_dir():
            return p
    return None


def _dlls(d: Path) -> set[str]:
    try:
        return {p.name.lower() for p in d.iterdir() if p.suffix.lower() == ".dll"}
    except OSError:
        return set()


def reference(steam_root: Path | None, app_id: int | None, protons: list) -> dict | None:
    """{"app_id", "prefix", "proton" (installed Proton name or None), "proton_dir", "version", "dlls64", "dlls32"}
    for a game Steam has run itself, else None."""
    if steam_root is None or not app_id:
        return None
    cd = _compatdata(Path(steam_root), int(app_id))
    if cd is None:
        return None
    proton, proton_dir = None, None
    try:
        info = (cd / "config_info").read_text("utf-8", "replace").splitlines()
    except OSError:
        info = []
    for ln in info:
        for p in protons:
            pp = str(getattr(p, "path", "") or "")
            if pp and ln.startswith(pp.rstrip("/") + "/"):
                proton, proton_dir = p.name, pp
                break
        if proton:
            break
    try:
        version = (cd / "version").read_text("utf-8", "replace").strip()
    except OSError:
        version = ""
    win = cd / "pfx" / "drive_c" / "windows"
    return {"app_id": int(app_id), "prefix": str(cd), "proton": proton, "proton_dir": proton_dir,
            "version": version, "dlls64": sorted(_dlls(win / "system32")), "dlls32": sorted(_dlls(win / "syswow64"))}


# redistributable installers that releases ship (and Steam runs on the first start) → winetricks verb
REDIST_FILES = [
    (r"vc_?redist.*(2015|2017|2019|2022|14\.|x64|x86).*\.exe|vc_redist\.x(64|86)\.exe", "vcrun2022",
     "Microsoft Visual C++ 2015–2022 runtime"),
    (r"vcredist.*2013.*\.exe|vcredist_x(64|86)_2013", "vcrun2013", "Microsoft Visual C++ 2013 runtime"),
    (r"vcredist.*2012.*\.exe", "vcrun2012", "Microsoft Visual C++ 2012 runtime"),
    (r"vcredist.*2010.*\.exe", "vcrun2010", "Microsoft Visual C++ 2010 runtime"),
    (r"vcredist.*2008.*\.exe", "vcrun2008", "Microsoft Visual C++ 2008 runtime"),
    (r"(dxsetup|dxwebsetup)\.exe|(jun2010|feb2010|aug2009).*\.cab", "d3dx9 d3dx10 d3dx11_43 xact",
     "DirectX 9–11 extensions (June 2010 redistributable)"),
    (r"(ndp4\d|dotnetfx4|dotnet-runtime|windowsdesktop-runtime).*\.exe", "dotnet48", ".NET Framework / runtime"),
    (r"physx.*\.(exe|msi)", "physx", "NVIDIA PhysX runtime"),
    (r"oalinst\.exe", "openal", "OpenAL audio"),
    (r"xnafx40.*\.msi", "xna40", "Microsoft XNA Framework 4.0"),
]
REDIST_DIRS = r"^(_?commonredist|_?redist(ributables?)?|redist|installers?|directx|vcredist|prerequisites|support)$"


def shipped_redists(root: Path, max_depth: int = 4, limit: int = 4000) -> list[tuple[str, str, str]]:
    """(verb, what, file) for redistributable installers in the game's folder."""
    import re
    out: dict[str, tuple[str, str, str]] = {}
    seen = 0
    stack = [(Path(root), 0)]
    while stack and seen < limit:
        d, depth = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            seen += 1
            if p.is_dir():
                if depth < max_depth and not p.name.startswith(".ezgd-backup-"):
                    # only descend into redist-looking folders below the top levels (a game can hold 100 000 files)
                    if depth < 1 or re.match(REDIST_DIRS, p.name, re.I) or re.match(REDIST_DIRS, d.name, re.I):
                        stack.append((p, depth + 1))
                continue
            for rx, verb, what in REDIST_FILES:
                if re.search(rx, p.name, re.I) and verb not in out:
                    out[verb] = (verb, what, str(p))
    return list(out.values())
