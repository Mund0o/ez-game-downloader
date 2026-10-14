"""Find and rank candidate game executables (and release launch scripts) inside an install folder.

Nothing here runs anything: files are only listed, read as text (scripts, readmes) and scored.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ezgd.catalog.normalize import tokens
from ezgd.platform import IS_WINDOWS
from ezgd.platform.launcher import WINDOWS_EXTS, is_native_linux_binary

_NOT_GAME = re.compile(
    r"(unins\d*|uninstall|crash ?handler|crashreport|crashpad|unitycrashhandler|vc_?redist|vcredist|dxsetup|"
    r"dxwebsetup|dotnet|directx|^setup|installer|redist|prereq|easyanticheat|eac_?|battleye|cefsharp|"
    r"^7z|unrar|quicksfv|notification_helper|helper|update|patcher|benchmark|dedicated_?server|"
    r"server|zfgamebrowser|ue4prereq|ueprereq|steamcmd|python|java|ffmpeg|"
    r"language|lang_?switch|langselect|config(urator|tool)?$|settings$|registry|regfix|activat|keygen)", re.I)
_SCRIPTS = (".bat", ".cmd")
_PREPARES = re.compile(r"^\s*(copy|xcopy|move|ren|rename|del|erase|mklink)\b", re.I)
_README = re.compile(r"(read ?_?me|instruction|how ?to|install|crack|play|launch|run)", re.I)
# play modes / variants recognisable from file names (CoD style t6mp / iw5sp included)
_MODES = [
    ("Multiplayer", re.compile(r"multi ?player|(?:^|[\W_\d])mp(?:$|[\W_])", re.I)),
    ("Zombies", re.compile(r"zombies?|(?:^|[\W_\d])zm(?:$|[\W_])", re.I)),
    ("Campaign", re.compile(r"single ?player|campaign|(?:^|[\W_\d])sp(?:$|[\W_])", re.I)),
    ("Co-op", re.compile(r"co-?op", re.I)),
    ("Vulkan", re.compile(r"vulkan|(?:^|[\W_])vk(?:$|[\W_])", re.I)),
    ("DirectX 12", re.compile(r"dx ?12|d3d ?12", re.I)),
    ("DirectX 11", re.compile(r"dx ?11|d3d ?11", re.I)),
    ("OpenGL", re.compile(r"opengl", re.I)),
    ("Offline", re.compile(r"offline", re.I)),
    ("Launcher", re.compile(r"launcher", re.I)),
    ("Editor / tools", re.compile(r"editor|modkit|mod ?tools|(?:^|[\W_])sdk(?:$|[\W_])", re.I)),
    ("Dedicated server", re.compile(r"dedicated|server", re.I)),
]


@dataclass
class Candidate:
    path: Path
    score: float
    reason: str
    kind: str = "game"                        # game | launch script | script | tool | installer
    modes: list[str] = field(default_factory=list)
    starts: str | None = None                 # for scripts: the exe they start
    prepares: list[str] = field(default_factory=list)   # for scripts: what they do first

    @property
    def windows(self) -> bool:
        return self.path.suffix.lower() in WINDOWS_EXTS

    @property
    def label(self) -> str:
        """One line for pickers, e.g. 'mp.cmd — Multiplayer · release launch script → t6mp.exe'."""
        bits = []
        if self.modes:
            bits.append(" / ".join(self.modes))
        if self.kind == "launch script":
            bits.append("release launch script")
        elif self.kind == "tool":
            bits.append("probably not the game")
        elif self.kind == "installer":
            bits.append("installer — use Install game")
        bits.append(self.reason)
        return f"{self.path.name}  — " + " · ".join(b for b in bits if b)

    @property
    def details(self) -> str:
        """Tooltip text: what the file is and, for scripts, what they do before starting the game."""
        lines = [str(self.path), self.reason]
        if self.starts:
            lines.append(f"Starts: {self.starts}")
        if self.prepares:
            lines.append("First runs: " + " · ".join(self.prepares[:6]))
        return "\n".join(lines)


def find_executables(root: Path, max_depth: int = 5, limit: int = 300) -> list[Path]:
    root = Path(root)
    if root.is_file():
        return [root]
    out: list[Path] = []
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in
                       ("_commonredist", "redist", "directx", "__installer", "support", "prereqs")]
        for fn in filenames:
            p = Path(dirpath) / fn
            low = fn.lower()
            if low.endswith((".exe",) + _SCRIPTS):
                out.append(p)
            elif not IS_WINDOWS and (low.endswith((".sh", ".x86_64", ".appimage")) or "." not in fn):
                if is_native_linux_binary(p):
                    out.append(p)
            if len(out) >= limit:
                return out
    return out


def _read_text(p: Path, limit: int = 64 * 1024) -> str | None:
    try:
        if p.stat().st_size > limit:
            return None
        return p.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def _exes_near(folder: Path) -> list[Path]:
    """.exe files in ``folder`` and one level below (scripts often start ``Game\\x.exe``)."""
    try:
        return [p for p in list(folder.glob("*")) + list(folder.glob("*/*")) if p.suffix.lower() == ".exe"]
    except OSError:
        return []


def script_info(script: Path) -> tuple[str | None, list[str]]:
    """(exe the script starts, lines it runs first) for a .bat/.cmd — read as text, never run."""
    text = _read_text(script)
    if text is None:
        return None, []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().lower().startswith(("rem ", "::"))]
    prepares = [ln for ln in lines if _PREPARES.match(ln) or ln.lower().startswith("if exist")]
    # longest name first, so "t6mp - Multiplayer Offline.exe" wins over a shorter file it contains
    exes = sorted(_exes_near(script.parent), key=lambda p: len(p.name), reverse=True)
    for ln in lines:
        low = ln.lower()
        if _PREPARES.match(ln) or low.startswith(("if ", "@echo", "echo", "cd", "pushd", "set ")):
            continue
        hit = next((p for p in exes if p.name.lower() in low), None)
        if hit:
            return hit.name, prepares
    return None, prepares


def release_launch_script(exe: Path) -> tuple[Path, list[str]] | None:
    """A .cmd/.bat next to ``exe`` that starts it after preparing files (e.g. Black Ops II's mp.cmd copies
    steam_api.dll_zmp over steam_api.dll first). Returns the script and its preparation lines."""
    for script in sorted(p for p in exe.parent.iterdir() if p.suffix.lower() in _SCRIPTS) \
            if exe.parent.is_dir() else []:
        starts, prepares = script_info(script)
        if starts and starts.lower() == exe.name.lower() and prepares:
            return script, prepares
    return None


def readme_mentions(root: Path, names: set[str]) -> dict[str, str]:
    """{exe file name (lower case): readme naming it} for files in ``names`` named in the release's text files."""
    out: dict[str, str] = {}
    if not root.is_dir() or not names:
        return out
    texts = [p for p in list(root.glob("*.txt")) + list(root.glob("*/*.txt")) if _README.search(p.stem)]
    for p in texts[:20]:
        text = (_read_text(p, 256 * 1024) or "").lower()
        for name in names:
            if name in text and not _NOT_GAME.search(Path(name).stem):
                out.setdefault(name, p.name)
    return out


def modes_of(name: str) -> list[str]:
    found = [label for label, rx in _MODES if rx.search(name)]
    if "Dedicated server" in found and "Editor / tools" in found:
        found.remove("Editor / tools")
    return found


def _hint_matches(p: Path, root: Path, hint: str) -> bool:
    h = hint.replace("\\", "/").strip("/ ").lower()
    if p.name.lower() == Path(h).name:
        try:
            rel = p.relative_to(root).as_posix().lower()
        except ValueError:
            return True
        return "/" not in h or rel.endswith(h)
    return False


def rank(paths: list[Path], root: Path, title: str, hint: str | None = None) -> list[Candidate]:
    title_toks = set(tokens(title))
    mentioned: dict[str, str] = {}
    exe_names = {p.name.lower() for p in paths if p.suffix.lower() == ".exe"}
    for folder in {root, *(p.parent for p in paths if p.suffix.lower() in _SCRIPTS)}:
        for k, v in readme_mentions(folder, exe_names).items():
            mentioned.setdefault(k, v)
    names_64 = {p.name.lower() for p in paths if re.search(r"(win64|x64|64bit|x86_64)", str(p.parent), re.I)}
    out: list[Candidate] = []
    for p in paths:
        score, why = 0.0, []
        name = p.stem
        kind = "game"
        if hint and _hint_matches(p, root, hint):
            score += 250                 # the source's own instructions outweigh every heuristic
            why.append("named on the game page")
        if p.name.lower() in mentioned:
            score += 40
            why.append(f"mentioned in {mentioned[p.name.lower()]}")
        if _NOT_GAME.search(name):
            score -= 80
            kind = "tool"
            if re.match(r"^setup", name, re.I) and p.suffix.lower() == ".exe" and \
                    next(p.parent.glob("*.bin"), None) is not None:
                kind = "installer"           # a repack's setup.exe: it installs the game, it is not the game
                why.append("the repack's installer (installs the game)")
            else:
                why.append("looks like a tool/installer")
        name_toks = set(tokens(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)))
        overlap = len(title_toks & name_toks)
        if overlap:
            score += 25 * overlap / max(1, len(title_toks))
            why.append("name matches title")
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            rel_parts = ("?",) * 6
        score -= 4 * (len(rel_parts) - 1)
        if any(part.startswith("_") for part in rel_parts[:-1]):
            score -= 30                  # release extras: "_Language Switcher", "_Redist", "_Tools"
            why.append("in an extras folder")
        if re.search(r"-win64-shipping$", name, re.I):
            score += 6
        if p.name.lower() in names_64 and re.search(r"(win32|x86(?!_64)|32bit)", str(p.parent), re.I):
            score -= 8                   # a 64-bit build of the same file exists
            why.append("32-bit (a 64-bit build exists)")
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        modes = modes_of(name)
        if "Dedicated server" in modes or "Editor / tools" in modes:
            score -= 40
            kind = "tool"
        starts, prepares = None, []
        if p.suffix.lower() in _SCRIPTS:
            starts, prepares = script_info(p)
            if starts and prepares:
                kind = "launch script"
                modes = modes or modes_of(Path(starts).stem)
                why.append(f"prepares files ({len(prepares)} step{'s' if len(prepares) > 1 else ''}) "
                           f"then starts {starts}")
            else:
                score -= 10              # a plain wrapper: the exe itself is the better choice
                if starts:
                    why.append(f"starts {starts}")
        else:
            score += min(15.0, size / (8 * 1024 * 1024))
        out.append(Candidate(p, score, ", ".join(why) or "executable", kind, modes, starts, prepares))

    # a launch script that prepares files must be used instead of the exe it starts: rank it just above it
    by_name = {c.path.name.lower(): c for c in out if c.path.suffix.lower() not in _SCRIPTS}
    for c in out:
        if c.kind == "launch script" and c.starts:
            target = by_name.get(c.starts.lower())
            if target is not None:
                c.score = max(c.score, target.score + 5)
                target.reason = (target.reason + ", " if target.reason != "executable" else "") + \
                    f"normally started by {c.path.name}"
    return sorted(out, key=lambda c: c.score, reverse=True)
