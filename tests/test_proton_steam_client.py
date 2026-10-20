"""Games that need the running Steam client (OnlineFix's multiplayer fix) keep Wine's bridge to it in every Proton
build. A friend's Risk of Rain 2 and Slay the Spire 2 (OnlineFix) failed with GE-Proton11-7 in the app but worked
with it in Steam: since 11-7, GE switches lsteamclient off when UMU_ID is set unless UMU_USE_STEAM=1; up to 11-6
(and Proton-CachyOS 11.0) the switch was the other way round, so the same games worked with 11-5. The fixtures are
the real Session.run code of both builds."""

import sys
from pathlib import Path

import pytest

from ezgd.library.models import CompatConfig
from ezgd.platform import proton, steam

from test_steam_overlay import library  # noqa: F401 — the LibraryManager fixture

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux launch semantics")

FIX = Path(__file__).parent / "fixtures" / "proton"
GE_11_5 = (FIX / "ge-11-5-run.txt").read_text()
GE_11_7 = (FIX / "ge-11-7-run.txt").read_text()
VALVE = "    def run(self):\n        argv = [g_proton.wine_bin, \"c:\\\\windows\\\\system32\\\\steam.exe\"]\n"


def test_how_each_build_treats_the_steam_client():
    assert proton._umu_mode_of(GE_11_7) == "flag"          # off unless UMU_USE_STEAM=1
    assert proton._umu_mode_of(GE_11_5) == "inverted"      # off only with UMU_USE_STEAM=1
    assert proton._umu_mode_of(VALVE) == "none"
    odd = "    if os.environ.get('X'):\n        append_to_env_str(self.env, 'WINEDLLOVERRIDES', 'lsteamclient=d', ';')\n"
    assert proton._umu_mode_of(odd) == "unknown"


def _tool(tmp_path, name: str, script: str) -> steam.ProtonInstall:
    d = tmp_path / name
    (d / "protonfixes").mkdir(parents=True)
    (d / "proton").write_text("#!/usr/bin/env python3\n" + script)
    return steam.ProtonInstall(name, d)


@pytest.mark.parametrize("script,want_flag,want_umu", [
    (GE_11_7, True, True),            # UMU_USE_STEAM=1: steam.exe, lsteamclient kept; protonfixes keeps the game id
    (GE_11_5, False, True),           # as before (umu.exe, lsteamclient kept) — UMU_USE_STEAM would switch it off
    (VALVE, False, True),
    ("    if os.environ.get('X'):\n        append_to_env_str(self.env, 'WINEDLLOVERRIDES', 'lsteamclient=d', ';')\n",
     False, False),                   # unknown: no UMU_ID at all — as Steam starts it
])
def test_a_steam_client_game_keeps_the_steam_client(tmp_path, script, want_flag, want_umu):
    tool = _tool(tmp_path, "GE-Proton", script)
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    kw = dict(steam_root=tmp_path / "Steam", prefix_dir=tmp_path / "pfx", steam_app_id=632360,
              compat=CompatConfig(use_runtime=False))
    cmd = proton.proton_command(exe, tool, steam_client=True, **kw)
    assert (cmd.env.get("UMU_USE_STEAM") == "1") is want_flag
    assert ("UMU_ID" in cmd.env) is want_umu
    assert "Steam client on" in cmd.description
    plain = proton.proton_command(exe, tool, **kw)                    # other games: unchanged
    assert plain.env["UMU_ID"] == "632360" and "UMU_USE_STEAM" not in plain.env
    assert "Steam client" not in plain.description


def test_umu_launcher_keeps_the_steam_client_too(tmp_path):
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    new = _tool(tmp_path, "GE-new", GE_11_7)
    old = _tool(tmp_path, "GE-old", GE_11_5)
    assert proton.umu_command(exe, "umu-run", prefix_dir=tmp_path, proton=new, steam_client=True
                              ).env["UMU_USE_STEAM"] == "1"
    assert proton.umu_command(exe, "umu-run", prefix_dir=tmp_path, proton=None, steam_client=True
                              ).env["UMU_USE_STEAM"] == "1"         # (umu downloads the latest GE)
    assert "UMU_USE_STEAM" not in proton.umu_command(exe, "umu-run", prefix_dir=tmp_path, proton=old,
                                                     steam_client=True).env
    assert "UMU_USE_STEAM" not in proton.umu_command(exe, "umu-run", prefix_dir=tmp_path, proton=new).env


def test_onlinefix_games_launch_with_the_steam_client(library, tmp_path, monkeypatch):
    """The whole path: an OnlineFix game in the Library with GE-Proton11-7 → UMU_USE_STEAM=1 in its launch; a game
    that does not need Steam keeps GE's default (Steam client off for non-Steam games)."""
    from ezgd.library.models import LaunchMethod, LibraryEntry, LibraryStatus
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    library.settings.set("steam_root", str(root))
    ge = _tool(tmp_path, "GE-Proton11-7", GE_11_7)
    monkeypatch.setattr(library, "protons", lambda *a, **k: [ge])

    def game(title: str, source: str) -> LibraryEntry:
        folder = tmp_path / "games" / title
        folder.mkdir(parents=True)
        (folder / "game.exe").write_bytes(b"MZ")
        return library.repo.insert(LibraryEntry(title=title, source=source, status=LibraryStatus.INSTALLED,
                                                install_path=str(folder), executable=str(folder / "game.exe"),
                                                executable_trusted=True, launch_method=LaunchMethod.PROTON,
                                                proton_version="GE-Proton11-7", steam_app_id=632360))
    of = game("Risk of Rain 2", "onlinefix")
    plan = library.launch_plan(of.id)
    assert plan.env["UMU_USE_STEAM"] == "1" and plan.env["UMU_ID"] == "632360"
    assert "Steam client on" in plan.description
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: "/usr/bin/umu-run" if name == "umu-run" else None)
    monkeypatch.setattr(library, "steam_root", lambda: None)          # no Steam: umu-launcher, the same rule
    plan = library.launch_plan(of.id)
    assert plan.env["UMU_USE_STEAM"] == "1" and "Steam client on" in plan.description
    monkeypatch.undo()
    library.settings.set("steam_root", str(root))
    monkeypatch.setattr(library, "protons", lambda *a, **k: [ge])
    other = game("Single Player", "steamrip")
    plan = library.launch_plan(other.id)
    assert "UMU_USE_STEAM" not in plan.env and "Steam client" not in plan.description


def test_the_assistant_names_the_missing_steam_client(tmp_path):
    """OnlineFix's message when Wine's Steam bridge is off (a friend's Slay the Spire 2 under GE-Proton11-7)."""
    from test_diagnose import make, run
    e, logp = make(tmp_path, "Proton: Executable is a unix path, launching with 'umu.exe'.\n"
                             "OnlineFix: Failed to load original steamclient. Error code: 998", seconds=4, code=1)
    found = {f["key"]: f for f in run(tmp_path, e, logp)["findings"]}
    assert "steam-client" in found and "Steam" in found["steam-client"]["title"]
    e, logp = make(tmp_path, "nothing about Steam here", seconds=4, code=1)
    assert "steam-client" not in {f["key"] for f in run(tmp_path, e, logp)["findings"]}
