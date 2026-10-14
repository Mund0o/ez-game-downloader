"""FitGirl Repacks: the source adapter and the install-first flow for repacks (setup.exe + data files)."""

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from conftest import fixture_bytes
from ezgd.catalog.models import Game, MethodKind
from ezgd.library import repack
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.platform import launcher
from ezgd.sources import ADAPTER_CLASSES
from ezgd.sources import fitgirl as fg
from ezgd.sources.base import ManualActionRequired, SourceError

API = "https://fitgirl-repacks.site/wp-json/wp/v2"
JSON = "application/json; charset=UTF-8"
POSTS = json.loads(fixture_bytes("fitgirl_posts.json"))


def post(pid: int) -> dict:
    return next(p for p in POSTS if p["id"] == pid)


# ------------------------------------------------------------------ adapter
def test_titles_split_into_name_and_version():
    assert fg.split_title("HOT WHEELS Infinite Rush &#8211; v1859562 + 6 DLCs") == \
        ("HOT WHEELS Infinite Rush", "v1859562 + 6 DLCs", None)
    assert fg.split_title("ELDEN RING NIGHTREIGN: Deluxe Edition, v1.03/v1.03.1 + 5 DLCs/Bonuses") == \
        ("ELDEN RING NIGHTREIGN: Deluxe Edition", "v1.03/v1.03.1 + 5 DLCs/Bonuses", None)
    assert fg.split_title("Mount &#038; Blade II: Bannerlord &#8211; Digital Deluxe Edition &#8211; v1.4.8 + 3 DLCs")[0] \
        == "Mount & Blade II: Bannerlord – Digital Deluxe Edition"
    assert fg.split_title("EA SPORTS FC 27 &#8211; v1.0.1 [Monkey Repack]") == ("EA SPORTS FC 27", "v1.0.1",
                                                                                 "Monkey Repack")
    assert fg.split_title("ACE COMBAT 8: WINGS OF THEVE + ACZ:TBW DLC") == ("ACE COMBAT 8: WINGS OF THEVE",
                                                                             "+ ACZ:TBW DLC", None)


def test_listing_skips_site_news_and_reads_cover_genres_size():
    assert fg.parse_listing_post(post(68049)) is None                # "Upcoming Repacks" (not a game)
    g = fg.parse_listing_post(post(82443))
    assert g.id == "fitgirl:82443" and g.title == "Heroes' Vow: Three Kingdoms" and g.version == "v1.0.2"
    assert g.genres == ["Strategy", "RPG"] and g.size_text == "3.3 GB" and g.size_bytes > 3_000_000_000
    assert g.cover_url.startswith("https://i7.imageban.ru/") and g.extra["installer"] == "fitgirl"


def test_details_magnet_first_then_host_parts():
    g = fg.parse_listing_post(post(29288))
    g = fg.parse_details(post(29288)["content"]["rendered"], g)
    assert g.extra["developer"] == "TaleWorlds Entertainment" and g.extra["languages"] == "RUS/ENG/MULTI13"
    assert g.extra["original_size"] == "74.5 GB" and g.extra["selective"] is True
    assert g.extra["release_group"].endswith("-RUNE") and "minutes" in g.extra["install_time"]
    assert g.extra["repack_number"] == 3074 and g.description.startswith("The horns sound")
    first = g.download_methods[0]
    assert first.kind is MethodKind.TORRENT and first.supported and first.url.startswith("magnet:?xt=urn:btih:")
    parts = [m for m in g.download_methods if m.file_name and m.file_name.endswith(".rar")]
    assert {m.label.split(" — ")[0] for m in parts} == {"DataNodes", "FuckingFast", "FileKeeper"}
    assert sum(1 for m in parts if m.label.startswith("FuckingFast")) == 23
    assert all(not m.supported and m.kind is MethodKind.EXTERNAL for m in parts)


async def test_listing_asks_only_for_repack_posts(routes, mock_fetcher):
    routes.add(f"{API}/tags*", b'[{"id": 66, "name": "Strategy"}, {"id": 53, "name": "3D"}]', content_type=JSON)
    routes.add(f"{API}/posts?*", json.dumps(POSTS), content_type=JSON, headers={"x-wp-totalpages": "3"})
    a = fg.FitGirlAdapter(mock_fetcher)
    page = await a.latest(1)
    assert [g.title for g in page.games] == ["Heroes' Vow: Three Kingdoms",
                                            "Mount & Blade II: Bannerlord – Digital Deluxe Edition"]
    assert page.has_more
    await a.latest(1, genre="Strategy")
    calls = [c for c in routes.calls if "/posts?" in str(c.url)]
    assert all(c.url.params["categories"] == "5" for c in calls)
    assert calls[-1].url.params["tags"] == "66"
    with pytest.raises(SourceError):
        await a.details(Game("fitgirl", "1", "x", "https://evil.example/x"))
    with pytest.raises(ManualActionRequired):
        await a.resolve(fg.download_methods(__import__("bs4").BeautifulSoup(
            post(82443)["content"]["rendered"], "lxml"))[1], Game("fitgirl", "1", "x", fg.BASE + "/x/"))


def test_fitgirl_is_built_in_everywhere():
    from ezgd.sources.custom import refused_platform
    from ezgd.ui import theme
    assert "fitgirl" in ADAPTER_CLASSES and theme.source_name("fitgirl") == "FitGirl"
    assert "already built into the app" in refused_platform("fitgirl-repacks.site")


# ------------------------------------------------------------------ repacks
def make_repack(folder: Path, md5_ok: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "setup.exe").write_bytes(b"MZ" + b"\0" * 2048)
    data = {"fg-01.bin": b"a" * 5000, "fg-02.bin": b"b" * 3000, "fg-optional-bonus.bin": b"c" * 10}
    for n, b in data.items():
        (folder / n).write_bytes(b)
    (folder / "MD5").mkdir()
    lines = [f"{hashlib.md5(b).hexdigest() if md5_ok or n != 'fg-02.bin' else '0' * 32} *..\\{n}"
             for n, b in data.items()]
    (folder / "MD5" / "fitgirl-bins.md5").write_text("; QuickSFV\n" + "\n".join(lines))
    return folder


def test_detect_repack_and_verify_md5(tmp_path):
    root = make_repack(tmp_path / "dl" / "Heroes Vow [FitGirl Repack]")
    rep = repack.detect(tmp_path / "dl")                         # found one level below the download folder
    assert rep.kind == "fitgirl" and rep.setup.name == "setup.exe" and rep.root == root
    assert rep.data_bytes == 8010 and rep.optional == ["fg-optional-bonus.bin"]
    assert repack.verify_md5(rep) == {"available": True, "checked": 3, "bad": [], "missing": []}
    bad = repack.detect(make_repack(tmp_path / "bad", md5_ok=False))
    assert repack.verify_md5(bad)["bad"] == ["fg-02.bin"]
    lone = tmp_path / "game"
    lone.mkdir()
    (lone / "setup.exe").write_bytes(b"MZ")                      # a lone setup.exe is not a repack
    assert repack.detect(lone) is None


def test_registry_records_and_windows_paths(tmp_path):
    pfx = tmp_path / "pfx"
    (pfx / "drive_c" / "Games" / "Heroes Vow").mkdir(parents=True)
    (pfx / "drive_c" / "Games" / "Heroes Vow" / "HeroesVow.exe").write_bytes(b"MZ" + b"\0" * 9000)
    (pfx / "system.reg").write_text(
        "WINE REGISTRY Version 2\n\n"
        "[Software\\\\Wow6432Node\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\Heroes Vow_is1] "
        f"{int(time.time())}\n#time=1dc\n"
        '"DisplayName"="Heroes\' Vow: Three Kingdoms"\n'
        '"Inno Setup: App Path"="C:\\\\Games\\\\Heroes Vow"\n'
        '"InstallLocation"="C:\\\\Games\\\\Heroes Vow\\\\"\n\n'
        "[Software\\\\Other] 1\n")
    recs = repack.uninstall_records(pfx)
    assert recs[0]["name"] == "Heroes' Vow: Three Kingdoms" and recs[0]["location"] == "C:\\Games\\Heroes Vow"
    assert repack.unix_path("C:\\Games\\Heroes Vow\\", pfx) == pfx / "drive_c" / "Games" / "Heroes Vow"
    assert repack.unix_path("Z:\\home\\me\\Games\\X", pfx) == Path("/home/me/Games/X")
    assert repack.windows_path(Path("/home/me/Games/X")) == "Z:\\home\\me\\Games\\X"
    # the preset folder was changed in the installer: the uninstall record says where it went
    found = repack.find_installed(tmp_path / "games" / "Heroes Vow", pfx, time.time() - 60, "Heroes' Vow")
    assert found == pfx / "drive_c" / "Games" / "Heroes Vow"


def test_install_target_never_the_repack_folder(tmp_path):
    games = tmp_path / "games"
    root = make_repack(games / "Heroes Vow")                      # extracted into the install folder
    assert repack.install_target(games, "Heroes Vow", root) == games / "Heroes Vow (installed)"
    assert repack.install_target(games, "Other", root) == games / "Other"


FAKE_INSTALLER = r"""
import sys, pathlib
dest = next(a for a in sys.argv if a.startswith("/DIR="))[5:]
dest = pathlib.Path(dest[2:].replace("\\", "/") if dest.startswith("Z:") else dest)
(dest / "Binaries").mkdir(parents=True, exist_ok=True)
(dest / "Binaries" / "HeroesVow.exe").write_bytes(b"MZ" + b"\0" * 20000)
(dest / "unins000.exe").write_bytes(b"MZ")
"""


@pytest.fixture
def library(db, settings, app_paths):
    from ezgd.database.repositories import GameRepository, LibraryRepository
    from ezgd.library.manager import LibraryManager
    events = []
    m = LibraryManager(LibraryRepository(db), GameRepository(db), settings, app_paths,
                       lambda n, p: events.append((n, p)))
    m.events = events
    return m


def test_installer_runs_only_when_confirmed_then_game_is_set_up(library, tmp_path, monkeypatch):
    root = make_repack(tmp_path / "downloads" / "Heroes Vow [FitGirl Repack]")
    e = library.repo.insert(LibraryEntry(title="Heroes Vow", status=LibraryStatus.EXTRACTED, source="fitgirl",
                                         install_path=str(root), download_path=str(root)))
    info = library.installer_info(e.id)
    assert info["state"] == "pending" and info["kind"] == "fitgirl" and info["md5"]
    assert info["target"] == str(Path(library.settings.settings.install_dir) / "Heroes Vow")
    assert library.get(e.id).setup["state"] == "pending"

    fake = tmp_path / "fake_setup.py"
    fake.write_text(FAKE_INSTALLER)
    seen = {}

    def plan(entry_id, overrides=None):
        seen.update(overrides or {})
        return launcher.LaunchPlan([sys.executable, str(fake)], str(root), {})
    monkeypatch.setattr(library, "launch_plan", plan)
    with pytest.raises(Exception, match="confirmation"):
        library.run_installer(e.id, confirmed=False)
    library.run_installer(e.id, confirmed=True)
    assert seen["executable"] == str(root / "setup.exe") and seen["compat"]["fixes"] == []
    assert library.installer_info(e.id)["state"] == "installing"
    deadline = time.time() + 20
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.05)
    library.reap()
    e = library.get(e.id)
    game = Path(library.settings.settings.install_dir) / "Heroes Vow"
    assert e.status is LibraryStatus.INSTALLED and e.install_path == str(game)
    assert e.executable == str(game / "Binaries" / "HeroesVow.exe") and not e.executable_trusted
    assert e.setup["state"] == "installed" and e.play_count == 0          # installing is not playing
    assert any(n == "notify" and p[0] == "Game installed" for n, p in library.events)
    left = library.installer_info(e.id)
    assert left["repack_left"] and left["repack_bytes"] == 8010
    freed = library.delete_repack(e.id)
    assert freed > 8000 and not root.exists() and game.exists()
    assert library.installer_info(e.id)["repack_left"] is False


def test_installer_closed_early_stays_pending(library, tmp_path, monkeypatch):
    root = make_repack(tmp_path / "downloads" / "G")
    e = library.repo.insert(LibraryEntry(title="G", status=LibraryStatus.EXTRACTED, source="fitgirl",
                                         install_path=str(root)))
    monkeypatch.setattr(library, "launch_plan",
                        lambda i, o=None: launcher.LaunchPlan([sys.executable, "-c", "pass"], str(root), {}))
    library.run_installer(e.id, confirmed=True)
    deadline = time.time() + 20
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.05)
    library.reap()
    info = library.installer_info(e.id)
    assert info["state"] == "pending" and "closed before it finished" in info["last_result"]
    assert library.get(e.id).install_path == str(root)


def test_repack_setup_is_ranked_as_installer(tmp_path):
    from ezgd.library.executables import find_executables, rank
    root = make_repack(tmp_path / "r")
    c = rank(find_executables(root), root, "Heroes Vow")
    assert c[0].kind == "installer" and "installs the game" in c[0].reason


def test_repack_installed_by_hand_becomes_playable(library, tmp_path):
    """Running setup.exe yourself and then picking the game's exe must leave the install step (Play, not Install)."""
    root = make_repack(tmp_path / "downloads" / "Hand [FitGirl Repack]")
    e = library.repo.insert(LibraryEntry(title="Hand", status=LibraryStatus.EXTRACTED, source="fitgirl",
                                         install_path=str(root), download_path=str(root)))
    assert library.installer_info(e.id)["state"] == "pending"
    game = tmp_path / "Games" / "Hand" / "Binaries" / "Win64"
    game.mkdir(parents=True)
    (game / "Hand.exe").write_bytes(b"MZ" + b"\0" * 5000)
    e = library.set_executable(e.id, game / "Hand.exe")
    assert e.setup["state"] == "installed" and e.install_path == str(tmp_path / "Games" / "Hand")
    info = library.installer_info(e.id)
    assert info["state"] == "installed" and info["repack_left"]


def test_installer_still_running_after_restart_stays_installing(library, tmp_path, monkeypatch):
    from ezgd.library import manager as mgr
    root = make_repack(tmp_path / "downloads" / "Busy")
    e = library.repo.insert(LibraryEntry(title="Busy", status=LibraryStatus.EXTRACTED, source="fitgirl",
                                         install_path=str(root), setup={"state": "installing", "root": str(root),
                                                                        "target": str(tmp_path / "t"), "started": 1}))
    monkeypatch.setattr(mgr, "_prefix_processes", lambda prefix: [12345])     # setup.exe still in the prefix
    assert library.installer_info(e.id)["state"] == "installing"
    assert library.get(e.id).setup["state"] == "installing"                  # not reset to "Install" again
    monkeypatch.setattr(mgr, "_prefix_processes", lambda prefix: [])
    assert library.installer_info(e.id)["state"] == "pending"                # it ended without installing
