"""Each kind of Proton gets its own prefix: GE-Proton11-7 in a prefix made by Proton 11.0 broke Slay the Spire 2."""

from types import SimpleNamespace

import pytest

from ezgd.database.repositories import GameRepository, LibraryRepository
from ezgd.library.manager import LibraryManager
from ezgd.library.models import LibraryEntry, LibraryStatus
from ezgd.platform import prefixguard as pg
from pe_builder import build_pe


@pytest.fixture
def library(db, settings, app_paths):
    return LibraryManager(LibraryRepository(db), GameRepository(db), settings, app_paths, lambda *a: None)


def proton(tmp_path, name, version):
    d = tmp_path / "protons" / name
    d.mkdir(parents=True)
    (d / "proton").write_text(f'#!/usr/bin/env python3\nCURRENT_PREFIX_VERSION="{version}"\n')
    return SimpleNamespace(name=name, path=d)


def test_versions_and_families():
    assert pg.family("GE-Proton11-7") == "ge" and pg.family("CachyOS-11.0-100") == "cachyos"
    assert pg.family("11.0-100") == "valve" and pg.family("10.1000-105") == "valve"
    assert not pg.compatible("11.0-100", "GE-Proton11-7")          # other kind: Proton cannot upgrade it
    assert pg.compatible("10.1000-105", "11.0-100")                # Valve → newer Valve: Proton upgrades it
    assert not pg.compatible("11.0-100", "10.1000-105")            # a downgrade breaks prefixes
    assert pg.compatible("GE-Proton11-5", "GE-Proton11-7") and pg.compatible(None, "GE-Proton11-7")


def test_friends_slay_the_spire_case(library, tmp_path, app_paths, monkeypatch):
    ge = proton(tmp_path, "Proton-GE Latest", "GE-Proton11-7")
    valve = proton(tmp_path, "Proton 11.0", "11.0-100")
    game = tmp_path / "Game"
    game.mkdir()
    (game / "Game.exe").write_bytes(build_pe(["kernel32.dll"], 64))
    e = library.repo.insert(LibraryEntry(title="STS2", status=LibraryStatus.INSTALLED, executable=str(game / "Game.exe"),
                                         install_path=str(game), proton_version="Proton-GE Latest"))
    monkeypatch.setattr(library, "protons", lambda refresh=False: [ge, valve])
    monkeypatch.setattr(library, "_proton_for", lambda en: ge if en.proton_version == "Proton-GE Latest" else valve)
    pfx = library.prefix_dir(e.id)
    save = pfx / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "SlayTheSpire2" / "save.dat"
    save.parent.mkdir(parents=True)
    save.write_text("act 3")
    (pfx / "version").write_text("GE-Proton11-7\n")            # GE stamped its version after the failed upgrade
    (app_paths.logs / f"game-{e.id}.log").write_text(
        f"Proton: Upgrading prefix from 11.0-100 to GE-Proton11-7 ({pfx}/)\n"
        "Proton: Prefix has an invalid version?! You may want to back up user files and delete this prefix.\n")
    msg = library.ensure_prefix_for(library.get(e.id))
    assert msg and "new prefix" in msg and "saves copied" in msg
    old = [p for p in pfx.parent.iterdir() if p.name.startswith(f"{pfx.name}.old-")]
    assert len(old) == 1                                       # the mixed-up prefix: kept as a backup only
    new_save = pfx / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "SlayTheSpire2" / "save.dat"
    assert new_save.read_text() == "act 3" and (pfx / pg.MARKER).read_text().strip() == "GE-Proton11-7"
    assert library.ensure_prefix_for(library.get(e.id)) is None   # next start: nothing to do
    # switching to Valve Proton: GE's prefix is kept aside, Valve gets its own; and back again reuses GE's
    (new_save).write_text("act 4")
    library.update(e.id, proton_version="Proton 11.0")
    assert "new prefix" in library.ensure_prefix_for(library.get(e.id))
    assert (pfx.parent / f"{pfx.name}.proton-ge").is_dir()
    assert new_save.read_text() == "act 4"                    # saves came along
    (pfx / "version").write_text("11.0-100\n")
    library.update(e.id, proton_version="Proton-GE Latest")
    assert "its own earlier" in library.ensure_prefix_for(library.get(e.id))
    assert (pfx / pg.MARKER).read_text().strip() == "GE-Proton11-7"


def test_bundled_dotnet_is_not_offered_dotnet48(tmp_path):
    from ezgd.library import diagnose as dg
    g = tmp_path / "Game"
    (g / "data_game_windows_x86_64").mkdir(parents=True)
    (g / "data_game_windows_x86_64" / "coreclr.dll").write_bytes(b"MZ")
    (g / "Game.exe").write_bytes(build_pe(["kernel32.dll"], 64))
    (g / "Game.pck").write_bytes(b"GDPC")
    e = LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"), install_path=str(g))
    run = dg.LastRun(started="x", text="System.IO.FileNotFoundException: Could not load file or assembly 'X'")
    c = dg.Context(e, g / "Game.exe", tmp_path / "pfx", run, "Godot", "", "", False, [], "GE", True, True, {})
    [f] = dg.r_dotnet(c)
    assert f.key == "dotnet-bundled" and f.fix is None and "Godot C#" in f.explanation


def test_winetricks_failure_is_explained(library, app_paths):
    e = library.repo.insert(LibraryEntry(title="W", status=LibraryStatus.INSTALLED))
    (app_paths.logs / f"game-{e.id}.log").write_text(
        "\n=== 2026-10-13 01:00:00 winetricks dotnet48\nExecuting w_do_call dotnet48\n"
        "curl: (6) Could not resolve host: download.microsoft.com\n")
    assert "internet" in library._winetricks_failure(e.id, "dotnet48")
