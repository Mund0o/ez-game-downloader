"""Title normalisation and genre canonicalisation."""

from __future__ import annotations

import re
import unicodedata

_TRADEMARKS = re.compile(r"[™®©℗℠]")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_SPACES = re.compile(r"\s+")

# Suffixes that sources append to every title (verified on the live sites).
_SOURCE_SUFFIXES = [
    re.compile(r"\s+по\s+сети\s*$", re.I),                     # OnlineFix: "<Game> по сети"
    re.compile(r"\s+free\s+download\b.*$", re.I),                # SteamRIP: "<Game> Free Download (v1.2)"
]
_TRAILING_PAREN = re.compile(r"\s*\(([^()]*)\)\s*$")


def clean_title(raw: str) -> tuple[str, str | None]:
    """Strip source boilerplate from a listing title.

    Returns ``(title, version_hint)``; e.g. ``"PEAK Free Download (v2.6.b + Co-op)"``
    → ``("PEAK", "v2.6.b + Co-op")``.
    """
    title = unicodedata.normalize("NFC", raw or "").strip()
    version = None
    m = re.search(r"\s+free\s+download\s*\(([^()]*)\)\s*$", title, re.I)
    if m:
        version = m.group(1).strip() or None
    for rx in _SOURCE_SUFFIXES:
        title = rx.sub("", title)
    title = _SPACES.sub(" ", title).strip(" -–—:")
    return title or (raw or "").strip(), version


def dedup_key(title: str) -> str:
    """Aggressively normalised key used to detect the same game across sources.

    Conservative on purpose: only cosmetic differences (case, punctuation,
    trademark signs, accents, '&' vs 'and') are ignored, so different editions
    or sequels never collapse into one entry.
    """
    # strip ™/® first: NFKD would otherwise turn "™" into the letters "TM"
    t = unicodedata.normalize("NFKD", _TRADEMARKS.sub("", title or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.casefold()
    t = t.replace("&", " and ").replace("’", "'").replace("'", "")
    t = _NON_WORD.sub(" ", t)
    t = _SPACES.sub(" ", t).strip()
    return t


# canonical genre names (English) keyed by lower-case source labels/slugs
_GENRES = {
    # OnlineFix category slugs (/games/<slug>/)
    "arcade": "Arcade", "survival": "Survival", "racing": "Racing", "fighting": "Fighting",
    "sandbox": "Sandbox", "rpg": "RPG", "simulator": "Simulation", "strategy": "Strategy",
    "horror": "Horror", "shooter": "Shooter", "puzzles": "Puzzle", "adventures": "Adventure",
    "vr": "VR",
    # SteamRIP category names
    "action": "Action", "adventure": "Adventure", "anime": "Anime", "building": "Building",
    "first-person shooter": "Shooter", "indie": "Indie", "multiplayer": "Multiplayer",
    "open world": "Open World", "role-playing game": "RPG", "sci-fi": "Sci-fi",
    "shooters": "Shooter", "simulation": "Simulation", "sports": "Sports",
    "virtual reality": "VR", "nudity": "Adult", "puzzle": "Puzzle",
}


def canonical_genre(label: str) -> str | None:
    if not label:
        return None
    key = label.strip().lower()
    if key in ("uncategorized", ""):
        return None
    return _GENRES.get(key, label.strip().title() if key.islower() else label.strip())


def tokens(text: str) -> list[str]:
    return [t for t in dedup_key(text).split(" ") if t]
