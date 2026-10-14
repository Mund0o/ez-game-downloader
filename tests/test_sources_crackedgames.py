import hashlib
import io
import zipfile

import httpx
import pytest

from conftest import fixture_bytes
from ezgd.catalog.models import Game, MethodKind
from ezgd.core.app_core import AppCore, ChecksumMismatchError
from ezgd.library.models import LibraryStatus
from ezgd.sources import ADAPTER_CLASSES
from ezgd.sources import crackedgames as cg
from ezgd.sources.base import LayoutChangedError, ManualActionRequired, SourceError

BASE = "https://cracked-games.org"
HYTALE = f"{BASE}/hytale-free-download"
HYTALE_SHA = "c987d34df92f1e70995df89d632222d007dd42378b1adf6a4ddaef99ca5952ed"


def hytale() -> Game:
    return Game("crackedgames", "hytale-free-download", "Hytale", HYTALE)


def test_listing_cards_are_normalized():
    games, more = cg.parse_listing(fixture_bytes("crackedgames_games.html").decode())
    assert more is True
    assert [g.title for g in games] == ["Stranded & Naked", "Clive Barker's Hellraiser: Revival", "Keep Watering",
                                        "Quarion"]
    g = games[1]
    assert g.id == "crackedgames:clive-barker-s-hellraiser-revival-free-download"
    assert g.url == f"{BASE}/clive-barker-s-hellraiser-revival-free-download"
    assert g.cover_url == f"{BASE}/uploads/cover/clive-barker-s-hellraiser-revival-cover.webp"
    assert g.genres == ["Action"] and g.size_text == "66.5 GB" and g.size_bytes > 66 * 1024 ** 3
    assert g.version == "Build 25778117"
    assert g.download_methods is None
    assert games[0].adult and not g.adult           # the site marks it 18+ / NSFW


def test_search_and_empty_pages():
    games, more = cg.parse_listing(fixture_bytes("crackedgames_search_hytale.html").decode())
    assert [g.title for g in games] == ["Hytale"] and more is False
    assert cg.parse_listing(fixture_bytes("crackedgames_search_empty.html").decode()) == ([], False)
    broken = '<html><body><div class="gc"><a href="/x">no title here</a></div></body></html>'
    with pytest.raises(LayoutChangedError):
        cg.parse_listing(broken)


def test_details_page():
    g = cg.parse_details(fixture_bytes("crackedgames_detail_hytale.html").decode(), hytale())
    assert g.title == "Hytale" and g.version == "Build 28052026"
    assert g.genres == ["RPG", "Adventure", "Indie"]
    assert g.platforms == ["Linux", "Windows", "macOS"] and g.release_date == "2026-01-13"
    assert g.size_text == "1.9 GB"
    assert g.extra["developer"] == "Hypixel Studios" and g.extra["release_group"] == "OFME"
    assert g.extra["archive_password"] == "cracked-games.org"
    assert g.extra["sha256"] == HYTALE_SHA and g.extra["crc32"] == "0AB15C82"
    assert g.extra["uploaded"] == "May 30, 2026"
    assert "only between cracked copies" in g.extra["multiplayer"]
    assert g.description.startswith("Hytale is the long-anticipated voxel sandbox-RPG")
    # the signed POST form is never automated: one browser-only method pointing at the game page
    [m] = g.download_methods
    assert m.kind is MethodKind.EXTERNAL and not m.supported and m.url == HYTALE
    assert m.checksum == f"sha256:{HYTALE_SHA}" and m.archive_password == "cracked-games.org"
    assert "browser" in m.note


def test_only_site_game_urls_are_accepted():
    assert cg.slug_from_url(HYTALE) == "hytale-free-download"
    for bad in ("https://evil.example/hytale-free-download", f"{BASE}/download", f"{BASE}/search?q=x",
                f"{BASE}/games/genre/rpg", f"{BASE}/../etc/passwd-free-download"):
        assert cg.slug_from_url(bad) is None


def test_registered_with_password():
    assert ADAPTER_CLASSES["crackedgames"] is cg.CrackedGamesAdapter
    assert cg.CrackedGamesAdapter.info.archive_password == "cracked-games.org"


async def test_adapter_requests_only_allowed_paths(routes, mock_fetcher):
    routes.add(f"{BASE}/games", fixture_bytes("crackedgames_games.html"))
    routes.add(f"{BASE}/games?page=2", fixture_bytes("crackedgames_search_empty.html"))
    routes.add(f"{BASE}/games?search=hytale", fixture_bytes("crackedgames_search_hytale.html"))
    routes.add(f"{BASE}/games/genre/rpg", fixture_bytes("crackedgames_search_hytale.html"))
    routes.add(HYTALE, fixture_bytes("crackedgames_detail_hytale.html"))
    a = cg.CrackedGamesAdapter(mock_fetcher)
    page = await a.latest()
    assert len(page.games) == 4 and page.has_more
    assert (await a.latest(2)).games == []
    assert [g.title for g in (await a.latest(1, "RPG")).games] == ["Hytale"]
    assert (await a.latest(1, "Horror")).games == []          # not a genre the site has: no request
    found = await a.search("hytale")
    assert [g.title for g in found.games] == ["Hytale"]
    assert (await a.search("h")).games == []
    g = await a.details(found.games[0])
    with pytest.raises(ManualActionRequired) as exc:
        await a.resolve(g.download_methods[0], g)
    assert exc.value.url == HYTALE
    with pytest.raises(SourceError):
        await a.details(Game("crackedgames", "x-free-download", "X", "https://evil.example/x-free-download"))
    paths = [r.url.path for r in routes.calls]
    assert not any(p.startswith(("/download", "/search", "/api", "/livewire")) for p in paths)
    assert all(r.method == "GET" for r in routes.calls)


async def test_refused_requests_are_explained(routes, mock_fetcher):
    routes.add(f"{BASE}/games", b"challenge", status=403)
    with pytest.raises(SourceError, match="browser"):
        await cg.CrackedGamesAdapter(mock_fetcher).latest()


# ------------------------------------------------------------ import a browser download
def archive() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Hytale/Hytale.exe", b"MZ" + b"\0" * 1000)
    return buf.getvalue()


@pytest.fixture
async def core(app_paths, routes):
    c = AppCore(app_paths, transport=httpx.MockTransport(routes), enable_torrents=False)
    c.set_emitter(lambda n, p: None)
    await c.start()
    c.settings.set("install_dir", str(app_paths.data.parent / "games"))
    yield c
    await c.shutdown()


async def _wait_extracted(core, entry_id):
    import asyncio
    for _ in range(200):
        e = core.library.get(entry_id)
        if e.status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED):   # INSTALLED: exe auto-chosen
            return e
        await asyncio.sleep(0.05)
    raise AssertionError(f"not extracted: {core.library.get(entry_id).status}")


async def test_import_verifies_published_sha256(core, tmp_path):
    data = archive()
    g = hytale()
    g.extra.update(sha256=hashlib.sha256(data).hexdigest(), archive_password="cracked-games.org")
    core.games.save(g)
    f = tmp_path / "Hytale.zip"
    f.write_bytes(data)
    r = await core.import_downloaded_file(g.id, str(f))
    assert r["verified"] is True and r["extracting"] is True
    e = await _wait_extracted(core, r["entry_id"])
    assert e.game_id == g.id and e.source == "crackedgames"
    assert (await core.entry_password(e.id)) == "cracked-games.org"
    assert any(p.name == "Hytale.exe" for p in __import__("pathlib").Path(e.install_path).rglob("*"))
    assert not e.executable_trusted                         # importing never runs or trusts anything


async def test_import_rejects_mismatch_unless_confirmed(core, tmp_path):
    g = hytale()
    g.extra["sha256"] = HYTALE_SHA
    core.games.save(g)
    f = tmp_path / "Hytale.zip"
    f.write_bytes(archive())
    with pytest.raises(ChecksumMismatchError):
        await core.import_downloaded_file(g.id, str(f))
    assert core.library.all() == []
    r = await core.import_downloaded_file(g.id, str(f), verify=False, extract=False)
    assert r["verified"] is False and not r["extracting"]
    assert core.library.get(r["entry_id"]).download_path == str(f)


async def test_import_without_published_checksum(core, tmp_path):
    core.games.save(Game("steamrip", "9", "SR Game", "https://steamrip.com/sr/"))
    f = tmp_path / "game.bin"
    f.write_bytes(b"not an archive")
    r = await core.import_downloaded_file("steamrip:9", str(f))
    assert r["verified"] is None and r["extracting"] is False
    with pytest.raises(SourceError):
        await core.import_downloaded_file("steamrip:9", str(tmp_path / "missing.zip"))


async def test_adult_games_hidden_by_default(core):
    games = cg.parse_listing(fixture_bytes("crackedgames_games.html").decode())[0]
    assert len(core.registry.visible(games)) == 3
    core.settings.set("hide_adult_games", False)
    assert len(core.registry.visible(games)) == 4
    assert core.settings.get("sources_enabled")["crackedgames"] is True


def test_run_instruction_becomes_executable_hint():
    page = fixture_bytes("crackedgames_detail_hytale.html").decode().replace(
        "Run the game executable (.exe) to play",
        "Run</span><span>Game\\start_protected_game.exe</span><span>to play")
    g = cg.parse_details(page, hytale())
    assert g.extra["launch_exe_hint"] == "Game\\start_protected_game.exe"
    assert "launch_exe_hint" not in cg.parse_details(fixture_bytes("crackedgames_detail_hytale.html").decode(),
                                                     hytale()).extra          # generic wording is ignored


async def test_imported_archive_deleted_after_extraction_when_enabled(core, tmp_path):
    """2026-10-11: “Delete the archive after a successful extraction” did nothing for files from the in-app
    browser / Import downloaded file (the extraction was not told where the file came from)."""
    import asyncio
    import zipfile
    from ezgd.library.models import LibraryStatus
    core.games.save(Game("steamrip", "11", "Del Game", "https://steamrip.com/del/"))
    dl = tmp_path / "Browser downloads"
    dl.mkdir()
    keep = dl / "other-game.zip"                                     # an unrelated file next to it stays
    for path in (dl / "Del.Game.zip", keep):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Del Game/Game.exe", b"MZ" + b"\0" * 100)
    for enabled in (False, True):
        core.settings.set("delete_archive_after_extract", enabled)
        f = dl / "Del.Game.zip"
        if not f.exists():
            with zipfile.ZipFile(f, "w") as zf:
                zf.writestr("Del Game/Game.exe", b"MZ" + b"\0" * 100)
        r = await core.import_downloaded_file("steamrip:11", str(f))
        for _ in range(200):
            if core.library.get(r["entry_id"]).status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED):
                break
            await asyncio.sleep(0.05)
        assert core.library.get(r["entry_id"]).status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED)
        assert f.exists() is (not enabled) and keep.exists()
