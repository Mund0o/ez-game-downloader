"""Finding a game's Steam App ID reliably — every Proton per-game fix, ProtonDB fix and steam_appid.txt needs it.

Order: the release's own files (steam_appid.txt, Steam-emulator configs), the catalog, ProtonDB's title index
(offline), then Steam's store search with title variants. A result is only accepted when it is unambiguous.
"""

from __future__ import annotations

import re
from pathlib import Path

from ezgd.catalog.normalize import dedup_key

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_EDITION = re.compile(r"\s*[-:–]?\s*\b(digital\s+)?(deluxe|ultimate|gold|goty|game of the year|complete|definitive|"
                      r"premium|standard|enhanced|anniversary|director'?s cut|special)(\s+edition)?\s*$", re.I)
_ONLINE = re.compile(r"\s+online\s*$", re.I)               # OnlineFix appends " Online" to titles


def key(title: str) -> str:
    """dedup_key with roman numerals as digits (Black Ops II == Black Ops 2)."""
    return " ".join(_ROMAN.get(w, w) for w in dedup_key(title).split())


def variants(title: str) -> list[str]:
    out = [title]
    t = _ONLINE.sub("", title)
    if t != title:
        out.append(t)
    for base in list(out):
        e = _EDITION.sub("", base)
        if e and e != base:
            out.append(e)
    return list(dict.fromkeys(x.strip() for x in out if x.strip()))


def title_keys(title: str) -> set[str]:
    return {key(v) for v in variants(title)}


def search_terms(title: str) -> list[str]:
    return variants(title)[:3]


def from_files(exe: Path, root: Path) -> int | None:
    """App ID written in the release itself (only when every source in the folder agrees)."""
    from ezgd.library import emulators as em
    found: set[int] = set()
    folders = {exe.parent}
    if root.is_dir():
        folders.update(p.parent for p in list(root.glob("**/steam_api*.dll"))[:10])
    for d in folders:
        for f in (d / "steam_appid.txt", d / "steam_settings" / "steam_appid.txt"):
            v = em._txt_id(f) if f.exists() else None
            if v:
                found.add(v)
        dll = next((d / n for n in em.STEAM_DLLS if (d / n).exists()), None)
        if dll is not None:
            setup = em.identify(dll)
            if setup.app_id and setup.kind != "onlinefix":     # OnlineFix may use a shared (fake) App ID
                found.add(setup.app_id)
    found.discard(480)                                        # Spacewar: the shared test ID, not the game
    return found.pop() if len(found) == 1 else None


def from_protondb(index: Path, keys: set[str]) -> int | None:
    from ezgd.library import protondb
    if not index.exists():
        return None
    games = (protondb._load(index).get("games") or {})
    hits = {int(app) for app, g in games.items() if g.get("t") and key(g["t"]) in keys}
    return hits.pop() if len(hits) == 1 else None
