"""Post-download pipeline, library management, launching and Steam integration."""

import os
import sys
import zipfile
from pathlib import Path

import pytest

from ezgd.database.repositories import (DownloadRepository, ExtractionRepository, GameRepository,
                                        LibraryRepository)
from ezgd.downloads.models import DownloadKind, DownloadStatus, DownloadTask
from ezgd.installation.pipeline import PostProcessor
from ezgd.library.executables import find_executables, rank
from ezgd.library.manager import LibraryError, LibraryManager
from ezgd.library.models import LaunchMethod, LibraryEntry, LibraryStatus
from ezgd.platform import launcher, steam, vdf


@pytest.fixture
def repos(db):
    return LibraryRepository(db), ExtractionRepository(db), GameRepository(db), DownloadRepository(db)


def make_task(repos, tmp_path, filename, **kw):
    task = DownloadTask(kind=DownloadKind.HTTP, title="Cool Game", uri="https://x.test/f",
                        dest_dir=str(tmp_path / "dl"), filename=filename, status=DownloadStatus.COMPLETED, **kw)
    return repos[3].insert(task)


async def test_completed_zip_is_extracted_into_install_dir(repos, settings, tmp_path):
    lib, ext, games, dls = repos
    (tmp_path / "dl").mkdir()
    archive = tmp_path / "dl" / "cool.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Cool Game/CoolGame.exe", b"MZ" + b"\0" * 4096)
        zf.writestr("Cool Game/unins000.exe", b"MZ")
    events = []
    post = PostProcessor(lib, ext, games, settings, lambda n, p: events.append((n, p)))
    task = make_task(repos, tmp_path, "cool.zip")
    entry = await post.on_download_completed(task, archive)
    entry = lib.get(entry.id)
    assert entry.status is LibraryStatus.EXTRACTED
    assert Path(entry.install_path) == Path(settings.settings.install_dir) / "Cool Game"
    assert (Path(entry.install_path) / "Cool Game" / "CoolGame.exe").exists()
    assert archive.exists()                                    # kept by default
    assert [e for e in events if e[0] == "extraction" and e[1]["status"] == "completed"]
    row = ext.for_library(entry.id)[0]
    assert row["status"] == "completed" and row["files_count"] == 2


async def test_archive_deleted_only_when_enabled(repos, settings, tmp_path):
    settings.set("delete_archive_after_extract", True)
    lib, ext, games, _ = repos
    (tmp_path / "dl").mkdir()
    archive = tmp_path / "dl" / "g.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", b"a")
    post = PostProcessor(lib, ext, games, settings, lambda *a: None)
    await post.on_download_completed(make_task(repos, tmp_path, "g.zip"), archive)
    assert not archive.exists()


async def test_corrupt_archive_keeps_entry_downloaded(repos, settings, tmp_path):
    lib, ext, games, _ = repos
    (tmp_path / "dl").mkdir()
    archive = tmp_path / "dl" / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("f.bin", os.urandom(50_000))
    archive.write_bytes(archive.read_bytes()[:20_000])
    events = []
    post = PostProcessor(lib, ext, games, settings, lambda n, p: events.append((n, p)))
    entry = await post.on_download_completed(make_task(repos, tmp_path, "bad.zip"), archive)
    entry = lib.get(entry.id)
    assert entry.status is LibraryStatus.DOWNLOADED
    row = ext.for_library(entry.id)[0]
    assert row["status"] == "failed" and "corrupt" in row["error"].lower()
    assert any(n == "notify" and "Extraction failed" in p[0] for n, p in events)


async def test_plain_folder_download_becomes_install_path(repos, settings, tmp_path):
    lib, ext, games, _ = repos
    folder = tmp_path / "dl" / "PreInstalled"
    folder.mkdir(parents=True)
    (folder / "game.exe").write_bytes(b"MZ")
    post = PostProcessor(lib, ext, games, settings, lambda *a: None)
    task = make_task(repos, tmp_path, "PreInstalled")
    entry = lib.get((await post.on_download_completed(task, folder)).id)
    assert entry.status is LibraryStatus.EXTRACTED and entry.install_path == str(folder)


async def test_interrupted_extraction_recovered(repos, settings, tmp_path):
    lib, ext, games, _ = repos
    e = lib.insert(LibraryEntry(title="X", status=LibraryStatus.EXTRACTING))
    dest = tmp_path / "games" / "X"
    (dest / ".ezgd-extract-1-2").mkdir(parents=True)
    xid = ext.create(None, e.id, str(tmp_path / "a.zip"), str(dest))
    ext.update(xid, status="running")
    post = PostProcessor(lib, ext, games, settings, lambda *a: None)
    assert post.recover() == 1
    assert ext.get(xid)["status"] == "failed"
    assert lib.get(e.id).status is LibraryStatus.DOWNLOADED
    assert not (dest / ".ezgd-extract-1-2").exists()


# ---------------------------------------------------------------- library
@pytest.fixture
def library(repos, settings, app_paths):
    lib, _, games, _ = repos
    return LibraryManager(lib, games, settings, app_paths, lambda *a: None)


def test_executable_ranking(tmp_path):
    root = tmp_path / "Game"
    (root / "Engine" / "Binaries").mkdir(parents=True)
    (root / "_CommonRedist").mkdir()
    for p, size in [("SuperGame.exe", 3_000_000), ("unins000.exe", 900_000), ("UnityCrashHandler64.exe", 1_000_000),
                    ("Engine/Binaries/SuperGame-Win64-Shipping.exe", 50_000_000), ("_CommonRedist/vcredist.exe", 10)]:
        (root / p).write_bytes(b"MZ" + b"\0" * min(size, 200_000))
    found = find_executables(root)
    assert not any("vcredist" in str(p) for p in found)
    ranked = rank(found, root, "Super Game")
    assert ranked[0].path.name in ("SuperGame.exe", "SuperGame-Win64-Shipping.exe")
    assert ranked[-1].path.name in ("unins000.exe", "UnityCrashHandler64.exe")
    hinted = rank(found, root, "Super Game", hint="UnityCrashHandler64.exe")
    assert hinted[0].path.name == "UnityCrashHandler64.exe"


def test_import_set_executable_and_trust(library, tmp_path):
    folder = tmp_path / "games" / "Thing"
    folder.mkdir(parents=True)
    exe = folder / "thing.exe"
    exe.write_bytes(b"MZ")
    e = library.import_folder("Thing", folder)
    assert e.status is LibraryStatus.EXTRACTED
    e = library.set_executable(e.id, exe)
    assert not e.executable_trusted
    with pytest.raises(LibraryError, match="confirmation"):
        library.launch(e.id, confirmed=False)
    e = library.mark_installed(e.id)
    assert e.status is LibraryStatus.INSTALLED
    with pytest.raises(LibraryError):
        library.set_executable(e.id, folder / "missing.exe")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_native_launch_after_confirmation(library, tmp_path):
    folder = tmp_path / "games" / "Native"
    folder.mkdir(parents=True)
    script = folder / "run.sh"
    marker = folder / "launched.txt"
    script.write_text(f"#!/bin/sh\necho ok > '{marker}'\n")
    e = library.import_folder("Native", folder)
    library.set_executable(e.id, script)
    with pytest.raises(launcher.LaunchError, match="not marked executable"):
        library.launch(e.id, confirmed=True)
    library.allow_execute(e.id)
    plan = library.launch(e.id, confirmed=True)
    assert plan.description == "native Linux"
    import time
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.read_text().strip() == "ok"
    e = library.get(e.id)
    assert e.executable_trusted and e.play_count == 1


def test_duplicate_installations_detected(library, tmp_path):
    a = tmp_path / "games" / "A"
    a.mkdir(parents=True)
    e1 = library.import_folder("PEAK", a)
    e2 = library.import_folder("PEAK™", tmp_path / "games")
    assert [d.id for d in library.duplicates(e2)] == [e1.id]
    assert [d.id for d in library.duplicates(e2.id)] == [e1.id]      # the UI passes ids


def test_remove_refuses_paths_outside_app_folders(library, tmp_path):
    outside = tmp_path / "personal"
    outside.mkdir()
    (outside / "important.doc").write_text("keep me")
    e = library.import_folder("Mine", outside)
    with pytest.raises(LibraryError, match="Refusing"):
        library.remove(e.id, delete_files=True)
    assert (outside / "important.doc").exists()
    inside = Path(library.settings.settings.install_dir) / "Deletable"
    inside.mkdir(parents=True)
    e2 = library.import_folder("Deletable", inside)
    assert library.remove(e2.id, delete_files=True) == [str(inside)]
    assert not inside.exists()
    root = Path(library.settings.settings.install_dir)
    e3 = library.import_folder("Root itself", root)
    with pytest.raises(LibraryError):
        library.remove(e3.id, delete_files=True)
    assert root.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux launch planning")
def test_launch_plans_linux(tmp_path, monkeypatch):
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    proton_dir = tmp_path / "Proton 9.0"
    proton_dir.mkdir()
    (proton_dir / "proton").write_text("#!/bin/sh\n")
    protons = [steam.ProtonInstall("Proton 9.0", proton_dir)]
    plan = launcher.build_plan(exe, method=LaunchMethod.AUTO, args="-windowed -lang en", protons=protons,
                               steam_root=tmp_path, prefix_dir=tmp_path / "pfx")
    # no toolmanifest → no runtime container; Steam's verb so the process lives as long as the game
    # Steam rule: options without %command% are appended, and the line is run by /bin/sh
    import shlex
    assert plan.argv == ["/bin/sh", "-c", f"{shlex.quote(str(proton_dir / 'proton'))} waitforexitandrun "
                                          f"{shlex.quote(str(exe))} -windowed -lang en"]
    assert plan.env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "pfx")
    monkeypatch.setattr(launcher.shutil, "which", lambda n: None)
    with pytest.raises(launcher.LaunchError, match="no Proton, umu-launcher or Wine"):
        launcher.build_plan(exe, method=LaunchMethod.AUTO, protons=[], prefix_dir=tmp_path / "pfx")
    with pytest.raises(launcher.LaunchError, match="cannot run natively"):
        launcher.build_plan(exe, method=LaunchMethod.NATIVE, prefix_dir=tmp_path / "pfx")


def test_binary_vdf_roundtrip_and_steam_shortcut(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "is_steam_running", lambda: False)      # Steam's files are only changed while closed
    original = {"shortcuts": {"0": {"appid": vdf.Int32(-123456), "AppName": "Existing", "Exe": '"/x"',
                                    "tags": {"0": "fav"}, "LastPlayTime": vdf.Int32(5)}}}
    data = vdf.binary_dumps(original)
    assert vdf.binary_loads(data) == original
    root = tmp_path / "Steam"
    cfg = root / "userdata" / "12345" / "config"
    cfg.mkdir(parents=True)
    (cfg / "shortcuts.vdf").write_bytes(data)
    (root / "steamapps").mkdir()
    exe = tmp_path / "Game" / "game.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    appid = steam.add_shortcut(root, "12345", name="My Game", exe=exe, start_dir=exe.parent, launch_options="--play 1")
    assert appid & 0x80000000
    parsed = vdf.binary_loads((cfg / "shortcuts.vdf").read_bytes())
    names = sorted(v["AppName"] for v in parsed["shortcuts"].values())
    assert names == ["Existing", "My Game"]
    assert list(cfg.glob("shortcuts.vdf.ezgd-backup-*"))
    # adding again updates instead of duplicating
    steam.add_shortcut(root, "12345", name="My Game", exe=exe, start_dir=exe.parent, launch_options="--play 1")
    parsed = vdf.binary_loads((cfg / "shortcuts.vdf").read_bytes())
    assert len(parsed["shortcuts"]) == 2
    assert parsed["shortcuts"]["0"] == original["shortcuts"]["0"]           # the user's shortcut untouched
    assert steam.find_steam_root(str(root)) == root.resolve()
    assert steam.steam_users(root) == ["12345"]


@pytest.mark.skipif(sys.platform != "linux", reason="Proton discovery is Linux-only")
def test_proton_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))     # ignore the developer's real Steam tools
    root = tmp_path / "Steam"
    for name in ("Proton 8.0", "Proton - Experimental", "Proton 9.0"):
        d = root / "steamapps" / "common" / name
        d.mkdir(parents=True)
        (d / "proton").write_text("")
    ge = root / "compatibilitytools.d" / "GE-Proton9-20"
    ge.mkdir(parents=True)
    (ge / "proton").write_text("")
    (root / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders"\n{\n "0"\n {\n  "path" "%s"\n }\n}\n' % root)
    names = [p.name for p in steam.find_proton_versions(root)]
    assert set(names) == {"Proton 8.0", "Proton - Experimental", "Proton 9.0", "GE-Proton9-20"}
    assert names == ["GE-Proton9-20", "Proton - Experimental", "Proton 9.0", "Proton 8.0"]


def test_text_vdf():
    data = vdf.text_loads('"a"\n{\n  "b" "c\\"d" // comment\n  "e" { "f" "g" }\n}\n')
    assert data == {"a": {"b": 'c"d', "e": {"f": "g"}}}
    with pytest.raises(vdf.VdfError):
        vdf.text_loads('"a" { "b" "c" } }')


def test_suspicious_onlinefix_release_detection(tmp_path):
    from ezgd.installation.pipeline import suspicious_release_files
    root = tmp_path / "Game"
    (root / "renpy").mkdir(parents=True)
    (root / "_CommonRedist" / "DirectX").mkdir(parents=True)
    (root / "_CommonRedist" / "DirectX" / "DXSETUP.exe").write_bytes(b"x")
    (root / "_CommonRedist" / "setup.exe").write_bytes(b"x")       # redist folders are fine
    (root / "Setup.exe").write_bytes(b"x")
    (root / "game.exe").write_bytes(b"x")
    assert suspicious_release_files(root) == ["Setup.exe", "renpy/"]
    clean = tmp_path / "Clean"
    clean.mkdir()
    (clean / "game.exe").write_bytes(b"x")
    assert suspicious_release_files(clean) == []


def test_launch_scripts_modes_and_readmes_are_recognised(tmp_path):
    root = tmp_path / "Release"
    game = root / "Call of Duty Black Ops II"
    (game / "_Language Switcher").mkdir(parents=True)
    (root / "bin" / "win32").mkdir(parents=True)
    (root / "bin" / "win64").mkdir(parents=True)
    mz = b"MZ" + b"\0" * 64
    for name in ("t6mp - Multiplayer Offline.exe", "t6mp.exe", "t6zm - Zombies Offline.exe", "t6sp.exe",
                 "_Language Switcher/Language Switcher.exe", "Redacted_Server.exe"):
        (game / name).write_bytes(mz)
    (game / "mp.cmd").write_text('@echo off\r\ncopy steam_api.dll_zmp steam_api.dll\r\n'
                                 '"t6mp - Multiplayer Offline.exe"\r\n')
    (game / "zm.cmd").write_text('@echo off\r\ncopy steam_api.dll_zmp steam_api.dll\r\n"t6zm - Zombies Offline.exe"')
    (game / "plain.bat").write_text("t6sp.exe\r\n")                      # no preparation: just a wrapper
    for arch in ("win32", "win64"):
        (root / "bin" / arch / "Tool.exe").write_bytes(mz)
    found = find_executables(root)
    ranked = rank(found, root, "Call of Duty: Black Ops II")
    by = {c.path.name: c for c in ranked}
    assert {c.path.name for c in ranked[:2]} == {"mp.cmd", "zm.cmd"}
    assert by["mp.cmd"].kind == "launch script" and by["mp.cmd"].starts == "t6mp - Multiplayer Offline.exe"
    assert by["mp.cmd"].prepares == ["copy steam_api.dll_zmp steam_api.dll"]
    assert by["mp.cmd"].modes == ["Multiplayer"] and by["zm.cmd"].modes == ["Zombies"]
    assert by["t6sp.exe"].modes == ["Campaign"] and "Offline" in by["t6mp - Multiplayer Offline.exe"].modes
    assert "normally started by mp.cmd" in by["t6mp - Multiplayer Offline.exe"].reason
    assert by["plain.bat"].kind == "game" and by["plain.bat"].score < by["t6sp.exe"].score
    assert by["Language Switcher.exe"].kind == "tool" and by["Redacted_Server.exe"].kind == "tool"
    assert ranked[-1].kind == "tool"
    tools = [c for c in ranked if c.path.name == "Tool.exe"]
    assert "win64" in str(tools[0].path) and "32-bit" in tools[1].reason
    assert "release launch script" in by["mp.cmd"].label and "steam_api" in by["mp.cmd"].details


def test_readme_and_page_hints(tmp_path):
    root = tmp_path / "Game"
    (root / "Game").mkdir(parents=True)
    mz = b"MZ" + b"\0" * 64
    for name in ("Launcher.exe", "Big Game.exe", "Game/start_protected_game.exe", "Game/eldenring.exe"):
        (root / name).write_bytes(mz)
    (root / "Read_Me_Instructions.txt").write_text("To play, run Launcher.exe as administrator.\n")
    ranked = rank(find_executables(root), root, "Big Game")
    assert ranked[0].path.name == "Launcher.exe" and "Read_Me_Instructions.txt" in ranked[0].reason
    hinted = rank(find_executables(root), root, "Big Game", hint="Game\\start_protected_game.exe")
    assert hinted[0].path.name == "start_protected_game.exe" and "named on the game page" in hinted[0].reason


def test_archive_deletion_only_takes_that_archive_and_its_parts(repos, settings, tmp_path):
    """Extracting Hades.II.zip deleted Hades.II.Soundtrack.zip next to it (names were matched by prefix)."""
    from ezgd.installation.pipeline import PostProcessor
    lib, ext, games, _ = repos
    d = tmp_path / "Browser downloads"
    d.mkdir()
    for name in ("Hades.II.zip", "Hades.II.Soundtrack.zip", "Hades.II.Fix.rar", "Big.part1.rar", "Big.part2.rar",
                 "Big.part3.rar", "Bigger.part1.rar", "Old.rar", "Old.r00", "Old.r01", "Split.7z.001",
                 "Split.7z.002", "Z.zip", "Z.z01", "notes.txt"):
        (d / name).write_bytes(b"x")
    post = PostProcessor(lib, ext, games, settings, lambda *a: None)
    post._delete_archive(d / "Hades.II.zip", d)
    post._delete_archive(d / "Big.part1.rar", d)
    post._delete_archive(d / "Old.rar", d)
    post._delete_archive(d / "Split.7z.001", d)
    post._delete_archive(d / "Z.zip", d)
    assert sorted(p.name for p in d.iterdir()) == ["Bigger.part1.rar", "Hades.II.Fix.rar", "Hades.II.Soundtrack.zip",
                                                   "notes.txt"]


def _steam_with_games(tmp_path, games: dict[int, str]) -> Path:
    root = tmp_path / "Steam"
    apps = root / "steamapps"
    (apps / "common").mkdir(parents=True)
    for appid, name in games.items():
        folder = name.replace(":", "")
        (apps / "common" / folder).mkdir()
        (apps / f"appmanifest_{appid}.acf").write_text(
            f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"StateFlags"\t\t"4"\n'
            f'\t"installdir"\t\t"{folder}"\n}}\n')
    return root


async def test_onlinefix_fix_only_game_goes_into_the_steam_game(repos, settings, tmp_path):
    """Little Nightmares III on OnlineFix offers only the fix: the game is the free Steam trial / Friend's Pass,
    and the fix is unpacked into that game's Steam folder (replaced files backed up)."""
    from conftest import fixture_bytes
    from ezgd.catalog.models import Game
    from ezgd.platform import steam
    from ezgd.sources import onlinefix as of
    lib, ext, games, _ = repos
    g = of.parse_details(fixture_bytes("onlinefix_detail_fixonly.html").decode("utf-8"),
                         Game("onlinefix", "17902", "Little Nightmares III",
                              "https://online-fix.me/games/officialservers/17902-little-nightmares-iii-po-seti.html"))
    assert g.extra["steam_base_game"] == "Little Nightmares III - Free Trial & Friend's Pass"
    games.save(g)
    settings.set("steam_root", str(tmp_path / "Steam"))
    (tmp_path / "dl").mkdir()
    fix = tmp_path / "dl" / "LittleNightmaresIII_Fix_Repair_Steam_Generic.zip"
    with zipfile.ZipFile(fix, "w") as zf:
        zf.writestr("OnlineFix64.dll", b"MZ fix")
        zf.writestr("LittleNightmaresIII/Binaries/Win64/steam_api64.dll", b"MZ onlinefix steam api")
    events = []
    post = PostProcessor(lib, ext, games, settings, lambda n, p: events.append((n, p)))
    task = make_task(repos, tmp_path, fix.name, game_id=g.id, source="onlinefix")
    # 1) the Steam game is not installed yet: nothing is unpacked, the Library says what to do
    entry = lib.get((await post.on_download_completed(task, fix)).id)
    assert entry.status is LibraryStatus.DOWNLOADED and "Install “Little Nightmares III - Free Trial" in entry.notes
    assert any(n == "notify" and "Install the game in Steam" in p[0] for n, p in events)
    assert entry.setup["state"] == "waiting_steam" and not post.fixes_ready_for_steam()
    # 2) installed in Steam (under the main game's name, next to the first game): the fix goes into it
    root = _steam_with_games(tmp_path, {1392860: "Little Nightmares III", 424840: "Little Nightmares"})
    assert steam.find_installed_game(root, g.extra["steam_base_game"])["appid"] == 1392860
    game_dir = root / "steamapps" / "common" / "Little Nightmares III"
    (game_dir / "LittleNightmaresIII" / "Binaries" / "Win64").mkdir(parents=True)
    (game_dir / "LittleNightmaresIII" / "Binaries" / "Win64" / "steam_api64.dll").write_bytes(b"MZ original")
    assert post.default_extract_dest(entry, fix) == game_dir
    [(ready, archive, folder)] = post.fixes_ready_for_steam()            # the core applies it by itself now
    assert ready.id == entry.id and archive == fix and folder == game_dir
    assert await post.extract(entry, fix, post.default_extract_dest(entry, fix), download_root=fix.parent)
    entry = lib.get(entry.id)
    assert not entry.setup and not post.fixes_ready_for_steam()          # applied: not again
    assert Path(entry.install_path) == game_dir and (game_dir / "OnlineFix64.dll").exists()
    backups = list(game_dir.glob(".ezgd-backup-*/LittleNightmaresIII/Binaries/Win64/steam_api64.dll"))
    assert backups and backups[0].read_bytes() == b"MZ original"           # Steam's own file kept
    assert 'WINEDLLOVERRIDES="OnlineFix64=n;' in entry.notes and "%command%" in entry.notes


async def test_clear_executable_is_chosen_after_extracting(repos, settings, tmp_path, app_paths):
    lib, ext, games, _ = repos
    (tmp_path / "dl").mkdir()
    events = []
    mgr = LibraryManager(lib, games, settings, app_paths, lambda *a: None)
    post = PostProcessor(lib, ext, games, settings, lambda n, p: events.append((n, p)))
    post.choose_executable = mgr.auto_executable
    archive = tmp_path / "dl" / "cool.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Cool Game/CoolGame.exe", b"MZ" + b"\0" * 4096)
        zf.writestr("Cool Game/UnityCrashHandler64.exe", b"MZ" + b"\0" * 64)
        zf.writestr("Cool Game/unins000.exe", b"MZ")
    entry = await post.on_download_completed(make_task(repos, tmp_path, "cool.zip"), archive)
    entry = lib.get(entry.id)
    assert entry.status is LibraryStatus.INSTALLED and Path(entry.executable).name == "CoolGame.exe"
    assert entry.executable_trusted is False                  # Play still asks once before the first start
    assert any(p[0] == "Ready to play" for n, p in events if n == "notify")
    # two equally likely programs: the user chooses
    archive2 = tmp_path / "dl" / "two.zip"
    with zipfile.ZipFile(archive2, "w") as zf:
        zf.writestr("Two/AlphaGame.exe", b"MZ" + b"\0" * 4096)
        zf.writestr("Two/BetaGame.exe", b"MZ" + b"\0" * 4096)
    t2 = repos[3].insert(DownloadTask(kind=DownloadKind.HTTP, title="Other Thing", uri="https://x.test/2",
                                      dest_dir=str(tmp_path / "dl"), filename="two.zip",
                                      status=DownloadStatus.COMPLETED))
    e2 = lib.get((await post.on_download_completed(t2, archive2)).id)
    assert e2.status is LibraryStatus.EXTRACTED and not e2.executable


def test_download_folder_is_the_games_own_folder(settings):
    from ezgd.core.settings import download_folder
    s = settings.settings
    assert download_folder(s, "Slay the Spire 2") == str(Path(s.install_dir) / "Slay the Spire 2")
    from ezgd.core.fsutil import safe_dirname
    assert download_folder(s, "A: B") == str(Path(s.install_dir) / safe_dirname("A: B"))
    settings.set("downloads_in_game_folder", False)
    assert download_folder(settings.settings, "X") == settings.settings.download_dir


async def test_one_folder_download_is_extracted_in_place_and_only_the_game_is_left(repos, settings, tmp_path):
    """The torrent lands in <Games>/<game>/ (with its own folder and a "Fix Repair" sub-folder), is extracted into
    the same folder, and with "delete the archive" on the archives and the emptied sub-folder are gone."""
    settings.set("delete_archive_after_extract", True)
    lib, ext, games, _ = repos
    folder = Path(settings.settings.install_dir) / "Cool Game"
    torrent_root = folder / "Cool Game"
    (torrent_root / "Fix Repair").mkdir(parents=True)
    main = torrent_root / "Cool.Game.v1-OFME.zip"
    with zipfile.ZipFile(main, "w") as zf:
        zf.writestr("Cool Game/CoolGame.exe", b"MZ" + b"\0" * 4096)
    fix = torrent_root / "Fix Repair" / "CoolGame_Fix_Repair.zip"
    with zipfile.ZipFile(fix, "w") as zf:
        zf.writestr("Cool Game/OnlineFix64.dll", b"MZ fix")
    post = PostProcessor(lib, ext, games, settings, lambda *a: None)
    task = repos[3].insert(DownloadTask(kind=DownloadKind.TORRENT, title="Cool Game", uri="magnet:?xt=x",
                                        dest_dir=str(folder), filename="Cool Game",
                                        status=DownloadStatus.COMPLETED, extract_dest=str(folder)))
    entry = lib.get((await post.on_download_completed(task, torrent_root)).id)
    assert (folder / "Cool Game" / "CoolGame.exe").exists()
    assert not main.exists() and not fix.exists() and not (torrent_root / "Fix Repair").exists()
    assert Path(settings.settings.install_dir).is_dir() and entry.install_path == str(folder)
    left = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))
    assert left == ["Cool Game", "Cool Game/CoolGame.exe"]                   # one folder, only the game
    assert "Fix Repair archive was deleted" in entry.notes


async def test_leftover_archives_are_listed_and_deleted(tmp_path, app_paths):
    import httpx
    from ezgd.core.app_core import AppCore
    core = AppCore(app_paths, transport=httpx.MockTransport(lambda r: httpx.Response(404)), enable_torrents=False)
    await core.start()
    try:
        core.settings.set("install_dir", str(tmp_path / "games"))
        core.settings.set("download_dir", str(tmp_path / "dl"))
        a = tmp_path / "dl" / "Old Game" / "Old.Game.rar"
        a.parent.mkdir(parents=True)
        a.write_bytes(b"R" * 1000)
        outside = tmp_path / "elsewhere" / "Mine.rar"
        outside.parent.mkdir()
        outside.write_bytes(b"R")
        fix = a.parent / "Fix Repair" / "OldGame_Fix_Repair.rar"
        fix.parent.mkdir()
        fix.write_bytes(b"Rar!\x1a\x07\x00" + b"F" * 3)
        e = core.library.repo.insert(LibraryEntry(title="Old Game", status=LibraryStatus.INSTALLED,
                                                  install_path=str(tmp_path / "games" / "Old Game"),
                                                  download_path=str(a.parent)))
        for arch in (a, outside):
            x = core.extraction_repo.create(None, e.id, str(arch), e.install_path)
            core.extraction_repo.update(x, status="completed")
        r = await core.leftover_archives()
        assert [i["archive"] for i in r["items"]] == [str(a), str(fix)] and r["size"] == 1010   # not outside
        d = await core.delete_leftover_archives()
        assert d == {"files": 2, "freed": 1010} and not a.exists() and not a.parent.exists() and outside.exists()
    finally:
        await core.shutdown()
