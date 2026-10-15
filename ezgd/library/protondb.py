"""ProtonDB community fixes: what players report made a game work (launch options, Proton versions).

ProtonDB publishes its reports as open data (https://github.com/bdefore/protondb-data, ODbL licence — "Data from
ProtonDB.com"). The app downloads the latest dump (≈70 MB, monthly), reads it as a stream and keeps a small index
per Steam App ID: how many players report it working, and the launch options / Proton versions used by players
who made it work.

Launch options from reports are untrusted text and they are run by /bin/sh, so only options made of plain
``NAME=value`` settings, known wrappers, ``%command%`` and game arguments are accepted; anything with shell
syntax (``;``, ``|``, ``&``, ``$(``, backticks, redirects …) is dropped.
"""

from __future__ import annotations

import gzip
import json
import re
import shlex
import tarfile
import threading
from collections import Counter, defaultdict
from pathlib import Path

LISTING = "https://api.github.com/repos/bdefore/protondb-data/contents/reports"
INDEX_FILE = "protondb-index.json.gz"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}
_SAFE_WRAPPERS = {"gamemoderun", "mangohud", "game-performance", "obs-gamecapture", "strangle"}
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}=[A-Za-z0-9_.,:=/+@%\-]{0,200}$")
_ARG = re.compile(r"^[-+/]?[A-Za-z0-9_.,:=/+@%\-]{1,120}$")
_SHELL = re.compile(r"[;&|`$<>(){}\\\\!*?\[\]~#\n]")
_LOCK = threading.Lock()
_CACHE: dict = {}


def dump_order(name: str) -> tuple[int, int, int]:
    """Sort key for dump names like ``reports_sep1_2026.tar.gz`` (newest last)."""
    m = re.match(r"reports_([a-z]{3})(\d+)_(\d{4})\.tar\.gz$", name)
    if not m:
        return (0, 0, 0)
    return (int(m.group(3)), _MONTHS.get(m.group(1), 0), int(m.group(2)))


def latest_dump(listing: list[dict]) -> dict | None:
    dumps = [d for d in listing if isinstance(d, dict) and dump_order(d.get("name", ""))[0]]
    return max(dumps, key=lambda d: dump_order(d["name"])) if dumps else None


def sanitize(options: str) -> str | None:
    """A community launch option string reduced to safe parts, or None when it is not safe/useful."""
    if not options or len(options) > 600 or "%command%" not in options:
        return None
    raw = options.replace('"', "").replace("'", "")
    if _SHELL.search(raw.replace("%command%", "")):
        return None
    try:
        tokens = shlex.split(options)
    except ValueError:
        return None
    if tokens.count("%command%") != 1:
        return None
    i = tokens.index("%command%")
    before, after = tokens[:i], tokens[i + 1:]
    for t in before:
        if not (_ENV.match(t) or t in _SAFE_WRAPPERS):
            return None
    for t in after:
        if not _ARG.match(t):
            return None
    env = [t for t in before if "=" in t]
    if not env and not after and not [t for t in before if t in _SAFE_WRAPPERS]:
        return None                                    # just "%command%": nothing to try
    if not env and not after:
        return None                                    # wrappers only (performance tools, not fixes)
    return " ".join(shlex.quote(t) if "=" not in t else t.split("=", 1)[0] + "=" + shlex.quote(t.split("=", 1)[1])
                    for t in before) + (" " if before else "") + "%command%" + (" " + " ".join(after) if after else "")


# settings players share that are not fixes (overlays, logging, frame caps, obsolete or Steam-only switches)
_COSMETIC = {"MANGOHUD", "MANGOHUD_CONFIG", "MANGOHUD_DLSYM", "DXVK_HUD", "PROTON_LOG", "DXVK_FRAME_RATE",
             "DXVK_ASYNC", "LD_PRELOAD", "ENABLE_VKBASALT", "VKD3D_DEBUG", "WINEDEBUG", "DXVK_LOG_LEVEL",
             "PROTON_ENABLE_WAYLAND", "PROTON_USE_WAYLAND", "WINE_USE_KWIN_HACKS", "SteamDeck", "OBS_VKCAPTURE"}


def is_fix(options: str) -> bool:
    """True when a (sanitized) option string changes something besides overlays/logging/frame caps."""
    try:
        tokens = shlex.split(options)
    except ValueError:
        return False
    i = tokens.index("%command%") if "%command%" in tokens else len(tokens)
    env = [t.split("=", 1)[0] for t in tokens[:i] if "=" in t]
    args = tokens[i + 1:]
    return any(k not in _COSMETIC for k in env) or any(a.lower() not in ("-fullscreen", "-windowed") for a in args)


def _iter_reports(fh):
    """Reports one by one from the dump's JSON array, without loading 500 MB into memory."""
    dec = json.JSONDecoder()
    buf = ""
    started = False
    while True:
        chunk = fh.read(1 << 20)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        pos = 0
        if not started:
            pos = buf.find("[")
            if pos < 0:
                buf = ""
                continue
            pos += 1
            started = True
        while True:
            while pos < len(buf) and buf[pos] in " \r\n\t,":
                pos += 1
            if pos >= len(buf) or buf[pos] == "]":
                break
            try:
                obj, end = dec.raw_decode(buf, pos)
            except ValueError:
                break                                  # incomplete object: read more
            yield obj
            pos = end
        buf = buf[pos:]


def build_index(tar_path: Path, out: Path) -> dict:
    """Read a dump and write the per-game index; returns stats."""
    total: Counter = Counter()
    works: Counter = Counter()
    opts: dict[str, Counter] = defaultdict(Counter)
    protons: dict[str, Counter] = defaultdict(Counter)
    titles: dict[str, str] = {}
    n = 0
    with tarfile.open(tar_path) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith(".json"))
        fh = tf.extractfile(member)
        for r in _iter_reports(fh):
            n += 1
            app = ((r.get("app") or {}).get("steam") or {}).get("appId")
            resp = r.get("responses") or {}
            if not app or not str(app).isdigit():
                continue
            app = str(app)
            total[app] += 1
            titles.setdefault(app, (r.get("app") or {}).get("title", ""))
            if resp.get("verdict") != "yes":
                continue
            works[app] += 1
            lo = sanitize(resp.get("launchOptions") or "")
            if lo:
                opts[app][lo] += 1
            pv = resp.get("customProtonVersion") or resp.get("protonVersion")
            if pv and pv != "Default" and len(pv) < 60:
                protons[app][pv] += 1
    index = {}
    for app, t in total.items():
        index[app] = {"t": titles.get(app, ""), "r": t, "ok": works[app],
                      "opts": opts[app].most_common(6) if app in opts else [],
                      "protons": protons[app].most_common(4) if app in protons else []}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"source": tar_path.name, "reports": n, "games": index}, fh)
    tmp.replace(out)
    with _LOCK:
        _CACHE.clear()
    return {"reports": n, "games": len(index), "with_fixes": sum(1 for v in index.values() if v["opts"])}


def _load(path: Path) -> dict:
    with _LOCK:
        if _CACHE.get("path") != str(path) or _CACHE.get("mtime") != (path.stat().st_mtime if path.exists() else 0):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            _CACHE.update(path=str(path), mtime=path.stat().st_mtime if path.exists() else 0, data=data)
        return _CACHE["data"]


def info(path: Path) -> dict:
    d = _load(path) if path.exists() else {}
    return {"available": bool(d), "source": d.get("source"), "reports": d.get("reports", 0),
            "games": len(d.get("games", {}))}


def for_game(path: Path, app_id: int | None) -> dict | None:
    if not app_id or not path.exists():
        return None
    return (_load(path).get("games") or {}).get(str(app_id))


def rating(game: dict | None, app_id: int | None = None) -> dict | None:
    """How well a game runs on Linux, from the players' reports in the index: ``pct`` of ``reports`` say it works.
    ``level``: great (≥ 85 %) · good (≥ 65 %) · mixed (≥ 40 %) · bad · few (under 3 reports — not enough to say)."""
    if not game or not game.get("r"):
        return None
    n, ok = int(game["r"]), int(game.get("ok") or 0)
    pct = ok / n
    level = "few" if n < 3 else "great" if pct >= 0.85 else "good" if pct >= 0.65 else "mixed" if pct >= 0.4 else "bad"
    return {"app": app_id, "pct": round(pct, 3), "reports": n, "level": level,
            "label": f"Linux {round(pct * 100)}%" if level != "few" else "Linux: few reports",
            "text": (f"{round(pct * 100)}% of {n} ProtonDB report{'s' if n != 1 else ''} say it runs on Linux"
                     if level != "few" else f"Only {n} ProtonDB report{'s' if n != 1 else ''} — not enough to say")}
