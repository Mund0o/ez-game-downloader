"""Wishlist: games the user wants, tracked across every source.

A wishlisted game is identified by its Steam App ID when it has one (any game can be found through Steam's store
search, even one no download site has yet) and by its normalised title. A game listed by a source is the same
game when it has the same Steam App ID, or the same title — compared without edition suffixes such as "Deluxe
Edition", but never across different Steam App IDs (so a sequel or a soundtrack never matches).
"""

from __future__ import annotations

import re

from ezgd.catalog.models import Game
from ezgd.catalog.normalize import dedup_key

WAITING, AVAILABLE, CLOSED, LIBRARY = "waiting", "available", "closed", "library"

_EDITION = re.compile(
    r"\s+(?:(?:digital\s+)?(?:deluxe|complete|definitive|ultimate|gold|premium|special|standard|anniversary|"
    r"collectors|enhanced|game of the year|goty|legendary|royal|directors cut|expanded)"
    r"(?:\s+edition)?|edition|bundle|\d+\s+dlcs?|all\s+dlcs?|with\s+all\s+dlcs?)$")


def loose_key(title: str) -> str:
    """Title key without edition/bundle suffixes ("Elden Ring Deluxe Edition" → "elden ring")."""
    k = dedup_key(title)
    for _ in range(3):
        new = _EDITION.sub("", k).strip()
        if new == k:
            break
        k = new
    return k


def same_game(item: dict, g: Game) -> bool:
    app = item.get("steam_app_id")
    if app and g.steam_app_id:
        return int(app) == int(g.steam_app_id)
    if g.dedup_key == item.get("dedup_key"):
        return True
    return bool(item.get("title")) and loose_key(g.title) == loose_key(item["title"])


def summarize(games: list[Game]) -> list[dict]:
    out, seen = [], set()
    for g in sorted(games, key=lambda x: (x.withdrawn, -(x.sort_time or 0))):
        if g.id in seen:
            continue
        seen.add(g.id)
        out.append({"game_id": g.id, "source": g.source, "title": g.title, "version": g.version,
                    "withdrawn": g.withdrawn, "url": g.url, "cover_url": g.cover_url})
    return out


def status_of(matches: list[dict], in_library: bool) -> str:
    if in_library:
        return LIBRARY
    if any(not m["withdrawn"] for m in matches):
        return AVAILABLE
    return CLOSED if matches else WAITING


def search_text(title: str) -> str:
    """What to type into a site's search box for a Steam name ("DOOM: The Dark Ages™" → "DOOM The Dark Ages")."""
    t = re.sub(r"[™®©]", "", title or "")
    t = re.sub(r"[:–—\-|]+", " ", t)
    return " ".join(t.split())[:60]
