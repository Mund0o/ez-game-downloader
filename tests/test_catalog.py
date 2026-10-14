from ezgd.catalog.models import DownloadMethod, Game, MethodKind
from ezgd.catalog.normalize import canonical_genre, clean_title, dedup_key
from ezgd.catalog.search import (Filters, InstallFilter, SortKey, filter_and_group, group_duplicates,
                                 relevance)


def g(source, gid, title, **kw):
    return Game(source=source, source_game_id=gid, title=title, url=f"https://{source}.test/{gid}", **kw)


def test_clean_title_strips_source_boilerplate():
    assert clean_title("PEAK Free Download (v2.6.b + Co-op)") == ("PEAK", "v2.6.b + Co-op")
    assert clean_title("Valley Peaks Free Download") == ("Valley Peaks", None)
    assert clean_title("DayZ (DayZavr) по сети") == ("DayZ (DayZavr)", None)
    assert clean_title("Shift At Midnight Online")[0] == "Shift At Midnight Online"


def test_dedup_key_ignores_cosmetics_only():
    assert dedup_key("PEAK™") == dedup_key("peak")
    assert dedup_key("Tom Clancy's Rainbow Six®") == dedup_key("Tom Clancys Rainbow Six")
    assert dedup_key("Pokémon & Friends") == dedup_key("pokemon and friends")
    assert dedup_key("Portal 2") != dedup_key("Portal")
    assert dedup_key("For The King II") != dedup_key("For The King")


def test_canonical_genres():
    assert canonical_genre("Role-playing Game") == "RPG"
    assert canonical_genre("simulator") == "Simulation"
    assert canonical_genre("Uncategorized") is None


def test_group_duplicates_by_title_and_steam_id():
    a = g("onlinefix", "1", "PEAK")
    b = g("steamrip", "2", "PEAK™", steam_app_id=3527290)
    c = g("steamrip", "3", "Other Game", steam_app_id=10)
    d = g("onlinefix", "4", "Other Game Renamed", steam_app_id=10)
    groups = group_duplicates([a, b, c, d, a])        # 'a' twice (overlapping pages)
    assert len(groups) == 2
    peak = next(x for x in groups if x.key == "peak")
    assert peak.is_duplicate and peak.sources == ["onlinefix", "steamrip"]
    other = next(x for x in groups if x is not peak)
    assert {e.id for e in other.entries} == {"steamrip:3", "onlinefix:4"}


def test_filters_and_sorting():
    torrent = DownloadMethod(MethodKind.TORRENT, "Torrent", "u", supported=True)
    games = [
        g("onlinefix", "1", "Alpha Quest", genres=["RPG"], download_methods=[torrent], published_at=3),
        g("steamrip", "2", "Beta Racer", genres=["Racing"], platforms=["PC"], published_at=5),
        g("steamrip", "3", "Gamma RPG", genres=["RPG"], platforms=["PC"], published_at=1),
    ]
    names = lambda groups: [x.title for x in groups]
    assert names(filter_and_group(games, Filters(sources={"steamrip"}), sort=SortKey.TITLE)) == ["Beta Racer", "Gamma RPG"]
    assert names(filter_and_group(games, Filters(genres={"RPG"}), sort=SortKey.RECENT)) == ["Alpha Quest", "Gamma RPG"]
    assert names(filter_and_group(games, Filters(platforms={"PC"}), sort=SortKey.RECENT)) == ["Beta Racer", "Gamma RPG"]
    assert names(filter_and_group(games, Filters(methods={"torrent"}))) == ["Alpha Quest"]
    installed = {dedup_key("Beta Racer")}
    assert names(filter_and_group(games, Filters(install=InstallFilter.INSTALLED), installed)) == ["Beta Racer"]
    assert "Beta Racer" not in names(filter_and_group(games, Filters(install=InstallFilter.NOT_INSTALLED), installed))
    assert names(filter_and_group(games, Filters(query="gamma"))) == ["Gamma RPG"]


def test_relevance_ordering():
    assert relevance("PEAK", "peak") == 1.0
    assert relevance("Peaks of Yore", "peak") > relevance("Moonlight Peaks", "peak") or \
        relevance("Peaks of Yore", "peak") >= 0.6
    assert relevance("Completely Different", "peak") < 0.39


def test_game_roundtrip_keeps_unknown_methods_distinct():
    a = g("x", "1", "T")
    assert a.download_methods is None
    b = Game.from_row(a.to_row())
    assert b.download_methods is None
    a.download_methods = []
    assert Game.from_row(a.to_row()).download_methods == []


def test_default_order_interleaves_sources_without_dates():
    from ezgd.catalog.models import Game
    from ezgd.catalog.search import Filters, SortKey, filter_and_group
    dated = [Game("steamrip", str(i), f"SR {i}", f"https://steamrip.com/{i}/", published_at=1000 - i) for i in range(3)]
    undated = [Game("crackedgames", f"cg{i}-free-download", f"CG {i}", f"https://cracked-games.org/cg{i}-free-download")
               for i in range(3)]
    titles = [g.title for g in filter_and_group(dated + undated, Filters(), sort=SortKey.RELEVANCE)]
    assert titles == ["SR 0", "CG 0", "SR 1", "CG 1", "SR 2", "CG 2"]
    recent = [g.title for g in filter_and_group(dated + undated, Filters(), sort=SortKey.RECENT)]
    assert recent[:3] == ["SR 0", "SR 1", "SR 2"]


def test_pages_are_parsed_in_a_helper_process_with_a_thread_fallback(monkeypatch):
    """Parsing in the app's own process held Python's lock for 10–70 ms per page, so the window stuttered when
    new pages arrived while scrolling; the helper process has its own interpreter."""
    import asyncio
    import os
    from concurrent.futures.process import BrokenProcessPool
    from ezgd.sources import parsing

    async def main():
        pid = await parsing.parse(parsing._warm)
        assert pid != os.getpid()                                # really another process
        assert not parsing._disabled

        class Broken:
            def submit(self, *a, **k):
                raise BrokenProcessPool("gone")
        monkeypatch.setattr(parsing, "_pool", Broken())
        monkeypatch.setattr(parsing, "_disabled", False)
        assert await parsing.parse(os.getpid) == os.getpid()     # fell back to a thread, same result
        assert parsing._disabled
    try:
        asyncio.run(main())
    finally:
        monkeypatch.setattr(parsing, "_disabled", False)
        parsing.shutdown()
