
import pytest

from conftest import fixture_bytes, make_torrent
from ezgd.catalog.models import Game, MethodKind
from ezgd.sources import onlinefix as of
from ezgd.sources.base import LayoutChangedError, ManualActionRequired, SourceError

HOME = fixture_bytes("onlinefix_home.html")
CP = "text/html; charset=windows-1251"


def detail_game(gid="17801", url="https://online-fix.me/games/adventures/17801-peak-po-seti.html"):
    return Game("onlinefix", gid, "PEAK", url)


def test_listing_cards_are_normalized():
    games, has_more, last = of.parse_listing(HOME.decode("cp1251"))
    assert [g.title for g in games] == ["DayZ (DayZavr)", "Gears of War E-Day Online", "DEADBOLT Online"]
    assert has_more and last == 98
    dayz = games[0]
    assert dayz.id == "onlinefix:17320"
    assert dayz.genres == ["Survival"]
    assert dayz.version == "1.29.163709"
    assert dayz.release_date == "13.12.2018"
    assert dayz.cover_url.startswith("https://online-fix.me/uploads/posts/")
    assert dayz.published_at and dayz.updated_at
    assert dayz.download_methods is None            # unknown until details are fetched
    # English-language card on the same page
    gears = games[1]
    assert gears.extra["network"] == "Microsoft Store" and gears.extra["official_servers"]
    deadbolt = games[2]
    assert deadbolt.extra["modes"] == ["Co-op"]     # multiplayer marked ✗ on the site


def test_featured_sections_are_the_real_carousels():
    sections = of.parse_featured(HOME.decode("cp1251"))
    assert [(s.key, s.source_label) for s in sections] == [("popular", "Популярное"), ("updates", "Обновления")]
    assert sections[0].games[0].title == "How to Fish"
    assert sections[1].games[0].updated_at is not None


def test_search_results_and_empty_search():
    games, more = of.parse_search(fixture_bytes("onlinefix_search.html").decode("cp1251"))
    assert [g.title for g in games] == ["DayZ (DayZavr)"] and not more
    assert of.parse_search(fixture_bytes("onlinefix_search_empty.html").decode("cp1251")) == ([], False)


def test_details_russian_page():
    g = of.parse_details(fixture_bytes("onlinefix_detail_peak.html").decode("cp1251"), detail_game())
    assert g.version == "2.6.b" and g.release_date == "16.06.2025"
    assert g.extra["archive_password"] == "online-fix.me"
    assert g.extra["launch_exe_hint"] == "PEAK.exe"
    assert g.extra["coop_players"] == 4
    assert g.description and g.description.startswith("PEAK")
    kinds = {m.label: (m.kind, m.supported) for m in g.download_methods}
    assert kinds["Torrent"] == (MethodKind.TORRENT, True)
    assert kinds["Online-Fix Drive"] == (MethodKind.EXTERNAL, False)
    assert kinds["OnlineFix server"] == (MethodKind.EXTERNAL, False)
    assert kinds["Online-Fix Hosters"][0] is MethodKind.MIRRORS
    assert all(m.archive_password == "online-fix.me" for m in g.download_methods)


def test_details_english_page():
    g = of.parse_details(fixture_bytes("onlinefix_detail_en.html").decode("cp1251"),
                         detail_game("18265", "https://online-fix.me/games/officialservers/18265-gears-of-war-e-day-online.html"))
    assert g.version == "1.1.3.0" and g.release_date == "06.10.2026"
    assert g.extra["network"] == "Microsoft Store"
    assert g.extra["multiplayer_players"] == 9
    assert g.description.startswith("Experience Emergence Day")
    assert any(m.kind is MethodKind.TORRENT for m in g.download_methods)


def test_hosters_page_marks_supported_and_dangerous_mirrors():
    methods = of.parse_hosters(fixture_bytes("onlinefix_hosters.html").decode(), "pw")
    pix = [m for m in methods if m.host == "pixeldrain.com"]
    assert pix and all(m.supported and m.kind is MethodKind.DIRECT for m in pix)
    assert any("fix files only" in m.label for m in pix)
    viking = [m for m in methods if m.host == "vikingfile.com"]
    assert viking and all(m.flagged_dangerous and not m.supported for m in viking)
    assert all(not m.supported for m in methods if m.host in ("gofile.io", "fileditchfiles.st", "filekeeper.net"))


def test_torrent_index_parsing_stays_on_host():
    html = fixture_bytes("onlinefix_torrent_index.html").decode()
    html = html.replace("</pre>", '<a href="https://evil.example/x.torrent">x.torrent</a>\n</pre>')
    entries = of.parse_torrent_index(html, "https://uploads.online-fix.me:2053/torrents/PEAK/")
    assert [e["name"] for e in entries] == ["PEAK.v2.6.b-OFME.torrent"]
    assert entries[0]["date"] is not None


def test_parked_domain_detected():
    with pytest.raises(LayoutChangedError, match="parking"):
        of.parse_listing(fixture_bytes("parked_onlinefix_me.html").decode())


@pytest.mark.parametrize("html", [
    "<html><body><p>maintenance</p></body></html>",
    "<html><body><div id='x'></div></body></html>",
])
def test_changed_layout_raises(html):
    with pytest.raises(LayoutChangedError):
        of.parse_listing(html)
    with pytest.raises(LayoutChangedError):
        of.parse_details(html, detail_game())


def test_cards_with_missing_fields_are_skipped_or_partial():
    html = """<html><body><div id="dle-content">
      <article class="news"><div class="article"><h2 class="title">No link</h2></div></article>
      <article class="news"><div class="article"><a class="big-link" href="/games/rpg/55-x-po-seti.html"></a>
        <h2 class="title">Bare Game по сети</h2></div></article>
      <article class="news"><div class="article"><a class="big-link" href="/not-a-game"></a>
        <h2 class="title">Weird</h2></div></article>
    </div></body></html>"""
    games, more, last = of.parse_listing(html)
    assert len(games) == 1
    g = games[0]
    assert g.title == "Bare Game" and g.cover_url is None and g.published_at is None and g.genres == ["RPG"]
    assert not more and last is None


def test_russian_dates():
    assert of.parse_ru_datetime("15 августа 2026, 12:35") is not None
    assert of.parse_ru_datetime("8 October 2026, 22:00") is not None
    assert of.parse_ru_datetime("Вчера, 22:00") is not None
    assert of.parse_ru_datetime("31 февраля 2026, 10:00") is None
    assert of.parse_ru_datetime("garbage") is None


async def test_adapter_end_to_end_with_mock_site(routes, mock_fetcher, tmp_path):
    base = "https://online-fix.me"
    routes.add(f"{base}/", HOME, content_type=CP)
    routes.add(f"{base}/page/2/", HOME, content_type=CP)
    routes.add(f"{base}/games/adventures/17801-peak-po-seti.html", fixture_bytes("onlinefix_detail_peak.html"),
               content_type=CP)
    routes.add("https://hosters.online-fix.me:2053/PEAK", fixture_bytes("onlinefix_hosters.html"))
    routes.add("https://uploads.online-fix.me:2053/torrents/PEAK/", fixture_bytes("onlinefix_torrent_index.html"))
    tor = make_torrent({"a.bin": b"x" * 40000}, tmp_path, "PEAK")
    routes.add("https://uploads.online-fix.me:2053/torrents/PEAK/PEAK.v2.6.b-OFME.torrent", tor,
               content_type="application/x-bittorrent")
    adapter = of.OnlineFixAdapter(mock_fetcher)

    page = await adapter.latest(1)
    assert len(page.games) == 3 and page.has_more
    sections = await adapter.featured()
    assert len(sections) == 2
    game = await adapter.details(detail_game())
    labels = [m.label for m in game.download_methods]
    assert labels[0] == "Torrent"                       # supported methods first
    assert any(l.startswith("Pixeldrain") for l in labels)
    assert not any(m.kind is MethodKind.MIRRORS for m in game.download_methods)
    torrent = next(m for m in game.download_methods if m.kind is MethodKind.TORRENT)
    res = await adapter.resolve(torrent, game)
    assert res.kind == "torrent" and res.torrent_bytes == tor and res.archive_password == "online-fix.me"
    drive = next(m for m in game.download_methods if m.label == "Online-Fix Drive")
    with pytest.raises(ManualActionRequired):
        await adapter.resolve(drive, game)
    # cache: second listing call does not hit the network
    before = len(routes.calls)
    await adapter.latest(1)
    assert len(routes.calls) == before


async def test_resolve_rejects_non_torrent_payload(routes, mock_fetcher):
    routes.add("https://uploads.online-fix.me:2053/torrents/X/", fixture_bytes("onlinefix_torrent_index.html"))
    routes.add("https://uploads.online-fix.me:2053/torrents/X/PEAK.v2.6.b-OFME.torrent", "<html>login</html>")
    adapter = of.OnlineFixAdapter(mock_fetcher)
    from ezgd.catalog.models import DownloadMethod
    m = DownloadMethod(MethodKind.TORRENT, "Torrent", "https://uploads.online-fix.me:2053/torrents/X/",
                       supported=True, resolver="onlinefix_torrent_index")
    with pytest.raises(SourceError, match="not a .torrent"):
        await adapter.resolve(m, detail_game())


async def test_details_refuses_foreign_urls(mock_fetcher):
    adapter = of.OnlineFixAdapter(mock_fetcher)
    with pytest.raises(SourceError):
        await adapter.details(Game("onlinefix", "1", "x", "https://evil.example/games/1-x.html"))


async def test_search_encodes_cyrillic_as_cp1251(routes, mock_fetcher):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        import httpx
        return httpx.Response(200, content=fixture_bytes("onlinefix_search_empty.html"), headers={"content-type": CP})
    routes.add_handler("https://online-fix.me/index.php*", handler)
    adapter = of.OnlineFixAdapter(mock_fetcher)
    page = await adapter.search("сталкер")
    assert page.games == []
    assert "story=%F1%F2%E0%EB%EA%E5%F0" in seen[0]
    assert (await adapter.search("ab")).games == []     # too short: no request
    assert len(seen) == 1


@pytest.mark.parametrize("code", [401, 403])
async def test_signed_in_only_torrent_becomes_browser_only(routes, mock_fetcher, code):
    """Some OnlineFix releases protect their torrent folder with HTTP auth (seen 2026-10-09 for
    'Minecraft Dungeons II'). That must become a browser fallback, never an attempt to authenticate."""
    from ezgd.catalog.models import DownloadMethod
    idx = "https://uploads.online-fix.me:2053/torrents/Minecraft%20Dungeons%20II/"
    routes.add(idx, "<html><title>401 Authorization Required</title></html>", status=code)
    adapter = of.OnlineFixAdapter(mock_fetcher)
    game = detail_game()
    m = DownloadMethod(MethodKind.TORRENT, "Torrent", idx, supported=True, resolver="onlinefix_torrent_index")
    with pytest.raises(ManualActionRequired, match="signed-in users") as ei:
        await adapter.resolve(m, game)
    assert ei.value.url == game.url                         # sends the user to the game page
    assert m.kind is MethodKind.EXTERNAL and not m.supported and m.resolver is None
    assert all("authorization" not in {k.lower() for k in c.headers} for c in routes.calls)


def test_hosters_page_without_mirrors_is_not_a_layout_change():
    assert of.parse_hosters(fixture_bytes("onlinefix_hosters_empty.html").decode(), "pw") == []
    with pytest.raises(LayoutChangedError):
        of.parse_hosters("<html><body><p>totally different</p></body></html>", "pw")


# ------------------------------------------------------------ closed guides (withdrawn games)
def test_closed_guide_detected_on_cards_and_page():
    games, _ = of.parse_search(fixture_bytes("onlinefix_search_closed.html").decode("cp1251"))
    by_title = {g.title: g for g in games}
    assert by_title["Minecraft Dungeons II"].withdrawn
    assert not by_title["Minecraft Dungeons"].withdrawn and by_title["Minecraft Dungeons"].withdrawn_known
    g = of.parse_details(fixture_bytes("onlinefix_detail_closed.html").decode("cp1251"),
                         detail_game("18260", "https://online-fix.me/games/adventures/18260-minecraft-dungeons-ii-po-seti.html"))
    assert g.withdrawn and "RUNE" in g.extra["closed_notice"]
    peak = of.parse_details(fixture_bytes("onlinefix_detail_peak.html").decode("cp1251"), detail_game())
    assert not peak.withdrawn and peak.withdrawn_known


def test_carousel_items_take_closed_state_from_page_cards():
    card = fixture_bytes("onlinefix_search_closed.html").decode("cp1251")
    import re as _re
    cards = _re.search(r'<div id="dle-content">(.*)</div></div></body>', card, _re.S).group(1)
    slider = ('<div class="horizontal-slider"><div class="title">Обновления</div><div class="container">'
              '<div><a href="/games/adventures/18260-minecraft-dungeons-ii-po-seti.html" title="Minecraft Dungeons II по сети">'
              '<img src="https://online-fix.me/x.jpg"/><p>Minecraft Dungeons II по сети</p></a></div>'
              '<div><a href="/games/adventures/18260-minecraft-dungeons-ii-po-seti.html" title="Minecraft Dungeons II по сети">'
              '<img src="https://online-fix.me/x.jpg"/><p>Minecraft Dungeons II по сети</p></a></div>'
              '<div><a href="/games/rpg/999-unknown-po-seti.html" title="Unknown по сети"><p>Unknown по сети</p></a></div>'
              '</div></div>')
    html = f'<html><body><div id="dle-content">{slider}{cards}</div></body></html>'
    (section,) = of.parse_featured(html)
    assert [g.title for g in section.games] == ["Minecraft Dungeons II", "Unknown"]      # duplicate collapsed
    assert section.games[0].withdrawn
    assert not section.games[1].withdrawn_known                                         # needs a page check


def test_reopened_guide_is_shown_again(db):
    from ezgd.database.repositories import GameRepository
    repo = GameRepository(db)
    closed = Game("onlinefix", "1", "X", "https://online-fix.me/games/rpg/1-x.html", extra={"guide_closed": True})
    repo.upsert_many([closed])
    reopened = Game("onlinefix", "1", "X", "https://online-fix.me/games/rpg/1-x.html", extra={"guide_closed": False})
    assert not repo.upsert_many([reopened])[0].withdrawn
    # a carousel entry (no information) must not clear a known state
    repo.upsert_many([closed])
    assert repo.upsert_many([Game("onlinefix", "1", "X", "https://online-fix.me/games/rpg/1-x.html")])[0].withdrawn


def test_closed_notice_translation():
    from ezgd.ui.dialogs.game_details import translate_closed_notice as t
    assert t("Руководство закрыто, используйте версию от RUNE и Parsec.") == \
        "The guide is closed, use the version from RUNE and Parsec."
    assert t("Руководство закрыто, бесплатная игра по сети более невозможна, покупайте игру.") == \
        "The guide is closed, free online play is no longer possible, buy the game."
    assert t("Руководство закрыто, что-то новое.") == "The guide is closed."
    assert of.CLOSED_RE.search("Руководство закрыто, бесплатная игра по сети более невозможна, покупайте игру.")
