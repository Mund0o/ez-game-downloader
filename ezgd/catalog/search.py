"""Unified search helpers: filtering, relevance, duplicate grouping and sorting."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import zip_longest
from typing import Iterable

from ezgd.catalog.models import Game, GameGroup
from ezgd.catalog.normalize import dedup_key, tokens


class SortKey(StrEnum):
    RELEVANCE = "relevance"
    TITLE = "title"
    RECENT = "recent"
    SOURCES = "sources"


class InstallFilter(StrEnum):
    ANY = "any"
    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"


@dataclass
class Filters:
    query: str = ""
    sources: set[str] = field(default_factory=set)      # empty = all
    genres: set[str] = field(default_factory=set)
    platforms: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)      # MethodKind values
    install: InstallFilter = InstallFilter.ANY


def relevance(title: str, query: str) -> float:
    """Simple, explainable score in [0, 1]. Exact > prefix > all tokens > some tokens."""
    q = dedup_key(query)
    if not q:
        return 0.0
    t = dedup_key(title)
    if t == q:
        return 1.0
    if t.startswith(q):
        return 0.9
    qt, tt = tokens(query), set(tokens(title))
    if not qt:
        return 0.0
    hits = sum(1 for tok in qt if tok in tt or any(w.startswith(tok) for w in tt))
    if hits == len(qt):
        return 0.6 + 0.2 * (len(q) / max(len(t), 1))
    return 0.4 * hits / len(qt)


def matches(game: Game, f: Filters, installed_keys: set[str]) -> bool:
    if f.sources and game.source not in f.sources:
        return False
    if f.genres and not (set(game.genres) & f.genres):
        return False
    if f.platforms and not (set(game.platforms) & f.platforms):
        return False
    if f.methods and not (game.method_kinds & f.methods):
        return False
    if f.install is InstallFilter.INSTALLED and game.dedup_key not in installed_keys:
        return False
    if f.install is InstallFilter.NOT_INSTALLED and game.dedup_key in installed_keys:
        return False
    if f.query and relevance(game.title, f.query) < 0.39:
        return False
    return True


def group_duplicates(games: Iterable[Game]) -> list[GameGroup]:
    """Group records describing the same game; Steam App IDs win over titles."""
    by_key: "OrderedDict[str, list[Game]]" = OrderedDict()
    app_to_key: dict[int, str] = {}
    seen_ids: set[str] = set()
    for g in games:
        if g.id in seen_ids:                      # same record twice (e.g. two pages overlap)
            continue
        seen_ids.add(g.id)
        key = g.dedup_key
        if g.steam_app_id and g.steam_app_id in app_to_key:
            key = app_to_key[g.steam_app_id]
        by_key.setdefault(key, []).append(g)
        if g.steam_app_id:
            app_to_key.setdefault(g.steam_app_id, key)
    return [GameGroup(k, v) for k, v in by_key.items()]


def filter_and_group(games: Iterable[Game], f: Filters, installed_keys: set[str] | None = None,
                     sort: SortKey = SortKey.RELEVANCE) -> list[GameGroup]:
    installed_keys = installed_keys or set()
    groups = group_duplicates(g for g in games if matches(g, f, installed_keys))
    return sort_groups(groups, sort, f.query)


def sort_groups(groups: list[GameGroup], sort: SortKey, query: str = "") -> list[GameGroup]:
    if sort is SortKey.TITLE:
        return sorted(groups, key=lambda g: g.title.casefold())
    if sort is SortKey.RECENT:
        return sorted(groups, key=lambda g: g.sort_time, reverse=True)
    if sort is SortKey.SOURCES:
        return sorted(groups, key=lambda g: (-len(g.sources), g.title.casefold()))
    if query:
        return sorted(groups, key=lambda g: (-relevance(g.title, query), -g.sort_time))
    return interleave_sources(sorted(groups, key=lambda g: g.sort_time, reverse=True))


def interleave_sources(groups: list[GameGroup]) -> list[GameGroup]:
    """Take turns between sources, keeping each source's own order.

    Some sources publish no dates (cracked-games.org listings); sorting purely by
    date would push all of their games below every dated one.
    """
    by_source: "OrderedDict[str, list[GameGroup]]" = OrderedDict()
    for g in groups:
        by_source.setdefault(g.primary.source, []).append(g)
    return [g for row in zip_longest(*by_source.values()) for g in row if g is not None]


def facet_values(games: Iterable[Game]) -> dict[str, list[str]]:
    genres, platforms = set(), set()
    for g in games:
        genres.update(g.genres)
        platforms.update(g.platforms)
    return {"genres": sorted(genres), "platforms": sorted(platforms)}
