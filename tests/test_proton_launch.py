"""Steam-style launching: launch options, Proton + Steam Linux Runtime chain, protonfixes ids,
compatibility options, DLL overrides, playtime tracking, and the v2 migration."""

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from ezgd.library.models import CompatConfig, LaunchMethod
from ezgd.platform import launcher, proton, steam

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux launch semantics")


# ------------------------------------------------------------ launch options
def test_launch_options_without_command_are_appended():
    o = launcher.parse_launch_options('-windowed -lang "en us"')
    assert o.args == ["-windowed", "-lang", "en us"] and not o.uses_command and not o.env


def test_launch_options_with_command(monkeypatch, tmp_path):
    wrapper = tmp_path / "gamemoderun"
    wrapper.write_text("#!/bin/sh\nexec \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    o = launcher.parse_launch_options("PROTON_LOG=1 DXVK_HUD=fps,gpuload gamemoderun %command% -dx11 -nolauncher")
    assert o.env == {"PROTON_LOG": "1", "DXVK_HUD": "fps,gpuload"}
    assert o.wrappers == [str(wrapper)] and o.args == ["-dx11", "-nolauncher"] and o.uses_command


@pytest.mark.parametrize("bad,msg", [
    ("gamescope-not-installed %command%", "not installed"),
    ("%command% > out.log", "shell operators"),
    ("A=1 %command% | tee log", "shell operators"),
    ("true || %command%", "shell operators"),
    ("true & %command%", "shell operators"),
    ("echo $(whoami) ; %command%", "shell operators"),
    ("true && true", "Use %command%"),
    ("&& %command%", "both sides"),
    ("%command% &&", "both sides"),
    ("not-a-real-program-xyz && %command%", "not installed"),
    ("%command% %command%", "only appear once"),
    ("foo%command%", "separate word"),
    ('-arg "unterminated', "Launch options"),
])
def test_invalid_launch_options(bad, msg):
    with pytest.raises(launcher.LaunchError, match=msg):
        launcher.parse_launch_options(bad)


# ------------------------------------------------------------ fake Steam tree
@pytest.fixture
def fake_steam(tmp_path):
    root = tmp_path / "Steam"
    common = root / "steamapps" / "common"
    rt = common / "SteamLinuxRuntime_4"
    rt.mkdir(parents=True)
    (rt / "_v2-entry-point").write_text("#!/bin/sh\n")
    (rt / "toolmanifest.vdf").write_text('"manifest"\n{\n "commandline" "/_v2-entry-point --verb=%verb% --"\n}\n')
    (root / "steamapps" / "appmanifest_4183110.acf").write_text(
        '"AppState"\n{\n "appid" "4183110"\n "name" "Steam Linux Runtime 4.0"\n "installdir" "SteamLinuxRuntime_4"\n}\n')
    ge = root / "compatibilitytools.d" / "GE-Proton10-1"
    (ge / "protonfixes" / "gamefixes-steam").mkdir(parents=True)
    (ge / "protonfixes" / "gamefixes-steam" / "3527290.py").write_text("# fix\n")
    (ge / "files" / "bin").mkdir(parents=True)
    (ge / "files" / "bin" / "wine").write_text("")
    for d in (ge, common / "Proton 11.0"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "proton").write_text("#!/bin/sh\n")
        (d / "toolmanifest.vdf").write_text('"manifest"\n{\n "commandline" "/proton %verb%"\n'
                                            ' "require_tool_appid" "4183110"\n}\n')
    exe = tmp_path / "Game" / "game.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    return root, exe


def protons(root):
    return {p.name: p for p in steam.find_proton_versions(root)}


def test_runtime_resolved_like_steam(fake_steam):
    root, _ = fake_steam
    p = protons(root)["GE-Proton10-1"]
    assert p.require_tool_appid == 4183110 and p.has_protonfixes and p.has_fix_for(3527290)
    rt = steam.find_runtime(root, 4183110)
    assert rt.name == "Steam Linux Runtime 4.0"
    assert rt.command() == [str(rt.path / "_v2-entry-point"), "--verb=waitforexitandrun", "--"]
    assert not protons(root)["Proton 11.0"].has_protonfixes


def test_steam_style_proton_command(fake_steam, tmp_path):
    root, exe = fake_steam
    ge = protons(root)["GE-Proton10-1"]
    compat = CompatConfig(options=["wined3d", "no_fsync", "log"], dll_overrides="winmm=n,b;version=n,b")
    plan = launcher.build_plan(exe, method=LaunchMethod.PROTON, protons=[ge], proton_name=ge.name,
                               steam_root=root, prefix_dir=tmp_path / "pfx-7", steam_app_id=3527290, compat=compat,
                               args="MANGOHUD=1 %command% -windowed", log_dir=tmp_path / "logs")
    rt = root / "steamapps" / "common" / "SteamLinuxRuntime_4"
    chain = [str(rt / "_v2-entry-point"), "--verb=waitforexitandrun", "--", str(ge.script), "waitforexitandrun",
             str(exe)]
    # exactly Steam: %command% → the quoted chain, the line runs in /bin/sh
    import shlex
    assert plan.argv == ["/bin/sh", "-c", "MANGOHUD=1 " + " ".join(shlex.quote(a) for a in chain) + " -windowed"]
    assert plan.shell_script == plan.argv[2]
    env = plan.env
    assert env["UMU_ID"] == "3527290"                      # protonfixes → gamefixes-steam/3527290.py
    assert "SteamAppId" not in env and "SteamGameId" not in env   # never exported unless opted in (breaks steam_api)
    assert env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "pfx-7")
    assert env["STEAM_COMPAT_TOOL_PATHS"] == f"{ge.path}:{rt}"
    assert env["PROTON_USE_WINED3D"] == "1" and env["PROTON_NO_FSYNC"] == "1"
    assert env["PROTON_LOG"] == "1" and env["PROTON_LOG_DIR"] == str(tmp_path / "logs")
    assert env["WINEDLLOVERRIDES"] == "winmm=n,b;version=n,b"
    assert "MANGOHUD" not in env                           # set by the shell line, as in Steam
    assert "Steam Linux Runtime 4.0" in plan.description and "protonfixes for app 3527290" in plan.description


def test_unknown_app_id_never_guessed_from_prefix_path(fake_steam, tmp_path):
    root, exe = fake_steam
    ge = protons(root)["GE-Proton10-1"]
    plan = launcher.build_plan(exe, method=LaunchMethod.PROTON, protons=[ge], steam_root=root,
                               prefix_dir=tmp_path / "game-42")
    # protonfixes would otherwise read "42" from STEAM_COMPAT_DATA_PATH as a Steam App ID
    assert plan.env["UMU_ID"] == "umu-0"


def test_compat_switches(fake_steam, tmp_path):
    root, exe = fake_steam
    ge = protons(root)["GE-Proton10-1"]
    c = CompatConfig(use_runtime=False, protonfixes=False, report_app_id=True)
    plan = launcher.build_plan(exe, method=LaunchMethod.PROTON, protons=[ge], steam_root=root,
                               prefix_dir=tmp_path / "p", steam_app_id=99, compat=c)
    assert plan.argv == [str(ge.script), "waitforexitandrun", str(exe)]
    assert plan.env["PROTONFIXES_DISABLE"] == "1" and plan.env["SteamAppId"] == "99"
    with pytest.raises(launcher.LaunchError, match="invalid DLL override"):
        launcher.build_plan(exe, method=LaunchMethod.PROTON, protons=[ge], steam_root=root,
                            prefix_dir=tmp_path / "p", compat=CompatConfig(dll_overrides="winmm=n,b; rm -rf"))


def hide_umu(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(launcher.shutil, "which", lambda n, *a, **k: None if n == "umu-run" else real(n, *a, **k))


def fake_umu(monkeypatch, tmp_path) -> str:
    umu = tmp_path / "bin" / "umu-run"
    umu.parent.mkdir(parents=True, exist_ok=True)
    umu.write_text("#!/bin/sh\n")
    umu.chmod(0o755)
    real = shutil.which
    monkeypatch.setattr(launcher.shutil, "which", lambda n, *a, **k: str(umu) if n == "umu-run" else real(n, *a, **k))
    return str(umu)


def test_missing_runtime_falls_back_to_direct(fake_steam, tmp_path, monkeypatch):
    hide_umu(monkeypatch)
    root, exe = fake_steam
    shutil.rmtree(root / "steamapps" / "common" / "SteamLinuxRuntime_4")
    ge = protons(root)["GE-Proton10-1"]
    plan = launcher.build_plan(exe, method=LaunchMethod.PROTON, protons=[ge], steam_root=root,
                               prefix_dir=tmp_path / "p")
    assert plan.argv[0] == str(ge.script)
    assert "not installed" in plan.description


def test_dll_override_suggestions(tmp_path):
    d = tmp_path / "G"
    d.mkdir()
    for n in ("game.exe", "winmm.dll", "OnlineFix64.dll", "steam_api64.dll", "Version.dll", "d3d11.dll"):
        (d / n).write_bytes(b"x")
    s = proton.suggest_dll_overrides(d / "game.exe")
    assert s.overrides == "version=n,b;winmm=n,b"      # d3d11 left to DXVK, steam_api64 has no builtin
    assert s.onlinefix and "OnlineFix" in s.explanation
    assert proton.suggest_dll_overrides(tmp_path / "missing" / "x.exe").overrides == ""


# ------------------------------------------------------------ migration v2
def test_v1_database_migrates_to_v2(tmp_path):
    from ezgd.database.db import Database
    from ezgd.database.migrations import MIGRATIONS
    path = tmp_path / "old.sqlite3"
    Database(path, migrations=MIGRATIONS[:1]).open().close()
    c = sqlite3.connect(path)
    c.execute("INSERT INTO library (title, dedup_key, status, added_at, updated_at) VALUES ('Old', 'old', "
              "'installed', 0, 0)")
    c.commit()
    c.close()
    db = Database(path, backup_dir=tmp_path / "bk").open()
    from ezgd.database.migrations import LATEST_VERSION
    assert db.schema_version == LATEST_VERSION and db.migrated_from == 1   # v1 → … → latest in one go
    from ezgd.database.repositories import LibraryRepository
    e = LibraryRepository(db).all()[0]
    assert e.title == "Old" and e.compat == {} and e.playtime_seconds == 0 and e.steam_app_id is None
    assert list((tmp_path / "bk").glob("pre-migration-v1-*"))
    db.close()


# ------------------------------------------------------------ playtime / stop
@pytest.fixture
def library(db, settings, app_paths):
    from ezgd.database.repositories import GameRepository, LibraryRepository
    from ezgd.library.manager import LibraryManager
    events = []
    m = LibraryManager(LibraryRepository(db), GameRepository(db), settings, app_paths,
                       lambda n, p: events.append((n, p)))
    m.events = events
    return m


def script_entry(library, tmp_path, body: str):
    folder = tmp_path / "games" / "Script"
    folder.mkdir(parents=True, exist_ok=True)
    s = folder / "run.sh"
    s.write_text("#!/bin/sh\n" + body + "\n")
    s.chmod(0o755)
    e = library.import_folder("Script", folder)
    library.set_executable(e.id, s)
    return e


def test_playtime_is_recorded(library, tmp_path):
    e = script_entry(library, tmp_path, "sleep 1.2")
    library.launch(e.id, confirmed=True)
    assert library.is_running(e.id)
    deadline = time.time() + 10
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.1)
    assert library.reap() == [e.id]
    e = library.get(e.id)
    assert e.playtime_seconds >= 1 and e.play_count == 1
    states = [p for n, p in library.events if n == "game_state"]
    assert states[0]["running"] is True and states[-1]["running"] is False and states[-1]["exit_code"] == 0


def test_stop_kills_game_and_children(library, tmp_path):
    marker = tmp_path / "child.pid"
    e = script_entry(library, tmp_path, f"sleep 60 & echo $! > '{marker}'; wait")
    library.launch(e.id, confirmed=True)
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    child = int(marker.read_text())
    with pytest.raises(Exception, match="already running"):
        library.launch(e.id, confirmed=True)
    with pytest.raises(Exception, match="Stop the game"):
        library.remove(e.id)
    library.stop(e.id)
    assert not library.is_running(e.id)
    time.sleep(0.2)
    import os
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)                        # the child in the same session is gone too


def test_update_validates_settings(library, tmp_path):
    e = script_entry(library, tmp_path, "true")
    e = library.update(e.id, launch_args="FOO=1 %command% -x", steam_app_id="3527290",
                       compat={"options": ["wined3d", "bogus"], "dll_overrides": " winmm=n,b ; version=n,b "})
    assert e.steam_app_id == 3527290 and e.compat["options"] == ["wined3d"]
    assert e.compat["dll_overrides"] == "winmm=n,b;version=n,b"
    from ezgd.library.manager import LibraryError
    with pytest.raises((LibraryError, launcher.LaunchError)):
        library.update(e.id, launch_args='%command% "unterminated')
    with pytest.raises(LibraryError):
        library.update(e.id, steam_app_id="-5")
    prev = library.preview(e.id, {"launch_args": "%command% --flag"})
    assert prev["ok"] and prev["command"].endswith("run.sh --flag")


# ------------------------------------------------------------ app id lookup
async def test_steam_app_id_lookup(app_paths, routes):
    import httpx
    from conftest import fixture_bytes
    from ezgd.catalog.models import Game
    from ezgd.core.app_core import AppCore
    routes.add("https://store.steampowered.com/api/storesearch/*", fixture_bytes("steam_storesearch_peak.json"),
               content_type="application/json")
    core = AppCore(app_paths, transport=httpx.MockTransport(routes), enable_torrents=False)
    await core.start()
    try:
        folder = app_paths.data.parent / "g"
        folder.mkdir()
        core.games.save(Game("onlinefix", "1", "PEAK", "https://online-fix.me/games/a/1-peak.html"))
        e = await core.library_call("import_folder", "PEAK", str(folder), "onlinefix:1")
        assert await core.lookup_steam_app_id(e.id) == 3527290
        assert core.library.get(e.id).steam_app_id == 3527290
        other = await core.library_call("import_folder", "Peak Darkness Remastered", str(folder))
        assert await core.lookup_steam_app_id(other.id) is None          # no exact match → no guess
        # known from the catalog (SteamRIP page) without any network request
        core.games.save(Game("steamrip", "2", "Stickility", "https://steamrip.com/s/", steam_app_id=111))
        e3 = await core.library_call("import_folder", "Stickility", str(folder))
        n = len(routes.calls)
        assert await core.lookup_steam_app_id(e3.id) == 111 and len(routes.calls) == n
    finally:
        await core.shutdown()


# ------------------------------------------------------------ real Proton (opt-in)
@pytest.mark.proton
def test_real_proton_in_steam_runtime(library, tmp_path):
    """Runs Wine's cmd.exe through the user's real Proton inside the Steam Linux Runtime and checks
    that it wrote a file into the game's prefix (launch-option arguments pass through the chain)."""
    exe_src = Path("/usr/lib/wine/x86_64-windows/cmd.exe")
    if not exe_src.exists() or not library.protons():
        pytest.skip("needs Proton and Wine's cmd.exe")
    folder = tmp_path / "games" / "Cmd"
    folder.mkdir(parents=True)
    exe = folder / "cmd.exe"
    shutil.copy(exe_src, exe)
    e = library.import_folder("Cmd", folder)
    library.set_executable(e.id, exe)
    library.update(e.id, launch_args=r"%command% /c 'echo ezgd-ok> C:\ezgd_ok.txt'")
    plan = library.launch_plan(e.id)
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 300                      # first run creates the prefix
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.5)
    assert not library.is_running(e.id), "Proton did not finish"
    library.reap()
    exits = [p for n, p in library.events if n == "game_state" and not p["running"]]
    assert exits and exits[-1]["exit_code"] == 0
    out = library.prefix_dir(e.id) / "pfx" / "drive_c" / "ezgd_ok.txt"
    log_tail = (library.paths.logs / f"game-{e.id}.log").read_text(errors="replace")[-1500:]
    assert out.exists() and "ezgd-ok" in out.read_text(errors="replace"), log_tail
    assert proton.prefix_ready(library.prefix_dir(e.id))
    print("\nlaunched with:", plan.description)


def test_umu_used_when_steam_runtime_missing(fake_steam, tmp_path, monkeypatch):
    root, exe = fake_steam
    shutil.rmtree(root / "steamapps" / "common" / "SteamLinuxRuntime_4")
    umu = fake_umu(monkeypatch, tmp_path)
    ge = protons(root)["GE-Proton10-1"]
    plan = launcher.build_plan(exe, method=LaunchMethod.PROTON, protons=[ge], steam_root=root,
                               prefix_dir=tmp_path / "pfx", steam_app_id=3527290,
                               compat=CompatConfig(options=["no_fsync"]), args="-windowed")
    import shlex
    assert plan.argv == ["/bin/sh", "-c", f"{shlex.quote(umu)} {shlex.quote(str(exe))} -windowed"]
    assert plan.env["PROTONPATH"] == str(ge.path) and plan.env["GAMEID"] == "3527290"
    assert plan.env["WINEPREFIX"] == str(tmp_path / "pfx") and plan.env["PROTON_NO_FSYNC"] == "1"
    assert plan.description.startswith("umu-launcher")


def test_umu_without_any_steam(tmp_path, monkeypatch):
    """No Steam at all: umu-launcher downloads GE-Proton + the Steam Runtime itself."""
    umu = fake_umu(monkeypatch, tmp_path)
    exe = tmp_path / "g" / "game.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    assert launcher.resolve_method(exe, LaunchMethod.AUTO, []) is LaunchMethod.PROTON
    plan = launcher.build_plan(exe, method=LaunchMethod.AUTO, protons=[], steam_root=None,
                               prefix_dir=tmp_path / "pfx")
    assert plan.argv == [umu, str(exe)]
    assert plan.env["PROTONPATH"] == "GE-Proton" and plan.env["GAMEID"] == "umu-default"


def test_no_way_to_run_windows_games(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda n, *a, **k: None)
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    with pytest.raises(launcher.LaunchError, match="System check"):
        launcher.build_plan(exe, method=LaunchMethod.PROTON, protons=[], prefix_dir=tmp_path / "p")


# ------------------------------------------------------------ dependency planning
def test_distro_detection():
    from ezgd.platform import deps
    assert deps.detect_family('ID=cachyos\nID_LIKE=arch\n') == "arch"
    assert deps.detect_family('ID=ubuntu\nID_LIKE=debian\n') == "debian"
    assert deps.detect_family('ID="nobara"\nID_LIKE="rhel centos fedora"\n') == "fedora"
    assert deps.detect_family('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n') == "suse"
    assert deps.detect_family('ID=gentoo\n') is None
    assert deps.multilib_enabled("[options]\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n")
    assert not deps.multilib_enabled("#[multilib]\n#Include = /etc/pacman.d/mirrorlist\n")


def test_install_plan_skips_installed_and_steam_when_umu_planned(monkeypatch):
    from ezgd.platform import deps
    monkeypatch.setattr(deps, "detect_family", lambda os_release=None: "arch")
    monkeypatch.setattr(deps, "_proton_capable", lambda: False)
    installed = {"rar", "winetricks"}
    fake = [deps.Tool(t.key, t.name, t.purpose, (lambda k=t.key: k in installed), t.packages, t.importance, t.note)
            for t in deps.LINUX_TOOLS]
    monkeypatch.setattr(deps, "LINUX_TOOLS", fake)
    monkeypatch.setattr(deps, "available_packages", lambda fam, pkgs: (list(pkgs), []))
    p = deps.plan()
    assert p.install == ["umu-launcher", "gamemode", "lib32-gamemode", "mangohud", "lib32-mangohud", "gamescope"]
    assert "steam" not in p.install and "unrar" not in p.install
    assert p.command[:4] == ["pacman", "-S", "--needed", "--noconfirm"]
    assert deps.install_command("debian", ["a"]) == ["apt-get", "install", "-y", "a"]


# ------------------------------------------------------------ chained commands (pre-launch / post-exit)
def test_pipewire_restart_before_launch_parses():
    o = launcher.parse_launch_options("systemctl --user restart pipewire wireplumber && %command%")
    assert o.uses_command and o.args == [] and not o.post
    (pre,) = o.pre
    assert Path(pre.argv[0]).name == "systemctl"
    assert pre.argv[1:] == ["--user", "restart", "pipewire", "wireplumber"] and pre.needs_success
    o = launcher.parse_launch_options("systemctl --user restart pipewire wireplumber;MANGOHUD=0 %command% -x")
    assert not o.pre[0].needs_success and o.env == {"MANGOHUD": "0"} and o.args == ["-x"]


def test_chain_semantics_and_env_per_command():
    o = launcher.parse_launch_options("FOO=1 true ; false && %command% -w ; X=2 true && true ;")
    assert [(Path(c.argv[0]).name, c.env, c.needs_success) for c in o.pre] == [
        ("true", {"FOO": "1"}, False), ("false", {}, True)]
    assert [(Path(c.argv[0]).name, c.env, c.needs_success) for c in o.post] == [
        ("true", {"X": "2"}, False), ("true", {}, True)]
    assert o.args == ["-w"]
    quoted = launcher.parse_launch_options("echo 'a;b && c' ; %command%")
    assert quoted.pre[0].argv[1:] == ["a;b && c"]          # operators inside quotes are plain text


def test_preview_shows_the_whole_chain(library, tmp_path):
    e = script_entry(library, tmp_path, "true")
    library.update(e.id, launch_args="systemctl --user restart pipewire wireplumber && %command% ; true")
    p = library.preview(e.id)
    exe = str(library.get(e.id).executable)
    assert p["command"] == f"systemctl --user restart pipewire wireplumber && {exe} ; true"
    assert "/bin/sh" in p["description"] and p["warnings"] == []
    library.update(e.id, launch_args="definitely-not-installed-tool %command%")
    assert "not installed" in library.preview(e.id)["warnings"][0]


def test_pre_and_post_commands_run(library, tmp_path):
    before, after, marker = tmp_path / "before.txt", tmp_path / "after.txt", tmp_path / "game-ran.txt"
    e = script_entry(library, tmp_path, f"touch '{marker}'")
    library.update(e.id, launch_args=f"touch '{before}' && %command% && touch '{after}'")
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 10
    while not after.exists() and time.time() < deadline:
        library.reap()
        time.sleep(0.1)
    assert before.exists() and marker.exists() and after.exists()
    assert "touch" in (library.paths.logs / f"game-{e.id}.log").read_text()


def test_failing_pre_command_blocks_launch_only_with_and(library, tmp_path):
    marker = tmp_path / "game-ran.txt"
    e = script_entry(library, tmp_path, f"touch '{marker}'")
    library.update(e.id, launch_args="false && %command%")      # the shell never reaches the game
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 10
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.05)
    library.reap()
    assert not marker.exists()
    assert [p for n, p in library.events if n == "game_state" and not p["running"]][-1]["exit_code"] == 1
    library.update(e.id, launch_args="false ; %command%")      # ; means: continue anyway
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists()


def test_post_command_after_failed_game(library, tmp_path):
    ok_marker, always_marker = tmp_path / "ok.txt", tmp_path / "always.txt"
    e = script_entry(library, tmp_path, "exit 3")
    library.update(e.id, launch_args=f"%command% && touch '{ok_marker}' ; touch '{always_marker}'")
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 10
    while not always_marker.exists() and time.time() < deadline:
        library.reap()
        time.sleep(0.1)
    assert always_marker.exists() and not ok_marker.exists()


def test_full_shell_features_like_steam(library, tmp_path):
    """Pipes, redirections and $(…) work, as they do in Steam's launch options."""
    out = tmp_path / "out.txt"
    e = script_entry(library, tmp_path, 'echo "game:$1:$GAME_VAR"')
    library.update(e.id, launch_args=f"GAME_VAR=$(echo hi) %command% \"$(echo arg)\" | tr a-z A-Z > '{out}'")
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 10
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.05)
    assert out.read_text().strip() == "GAME:ARG:HI"


def test_command_is_quoted_safely(library, tmp_path):
    """Paths with spaces, quotes and $ survive %command% substitution untouched."""
    folder = tmp_path / "games" / "it's a $GAME (x)"
    folder.mkdir(parents=True)
    exe = folder / "run me.sh"
    marker = tmp_path / "ran.txt"
    exe.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    exe.chmod(0o755)
    e = library.import_folder("Odd", folder)
    library.set_executable(e.id, exe)
    library.update(e.id, launch_args="%command%")
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists()


def test_syntax_errors_rejected_before_saving(library, tmp_path):
    from ezgd.library.manager import LibraryError
    e = script_entry(library, tmp_path, "true")
    for bad in ('%command% "unterminated', "if %command%", "%command% &&"):
        with pytest.raises(LibraryError, match="syntax error"):
            library.update(e.id, launch_args=bad)


# ------------------------------------------------------------ compatibility advisor
def _godot_onlinefix_game(tmp_path, library):
    """Mirror of the real Slay the Spire 2 OnlineFix layout (2026-10-09)."""
    d = tmp_path / "games" / "StS2" / "Slay the Spire 2"
    d.mkdir(parents=True)
    for n in ("SlayTheSpire2.exe", "winmm.dll", "OnlineFix64.dll", "SteamOverlay64.dll", "dlllist.txt"):
        (d / n).write_bytes(b"MZ")
    (d / "OnlineFix.ini").write_text("[Main]\nRealAppId=2868840\nFakeAppId=480\n")
    (d / "launch_vulkan.bat").write_text('@echo off\r\n"%~dp0SlayTheSpire2.exe" --rendering-driver vulkan %*\r\n')
    e = library.import_folder("Slay the Spire 2", d.parent)
    return e, d / "SlayTheSpire2.exe"


def test_onlinefix_overrides_applied_automatically(library, tmp_path):
    e, exe = _godot_onlinefix_game(tmp_path, library)
    e = library.set_executable(e.id, exe)
    assert "game_dlls" in e.compat_config.fixes                     # switched on automatically
    assert not [s for s in library.suggestions(e.id) if s["key"] == "dll-overrides"]
    assert library.launch_plan(e.id).env["WINEDLLOVERRIDES"] == "winmm=n,b"


def test_advisor_suggestions_for_the_slay_the_spire_2_case(library, tmp_path):
    e, exe = _godot_onlinefix_game(tmp_path, library)
    library.set_executable(e.id, exe)
    library.update(e.id, launch_args="systemctl --user restart pipewire wireplumber",
                   compat={"dll_overrides": "", "fixes": []})    # the user's original settings
    logs = library.prefix_dir(e.id) / "pfx/drive_c/users/steamuser/AppData/Roaming/SlayTheSpire2/logs"
    logs.mkdir(parents=True)
    (logs / "godot.log").write_text(
        "[ERROR] Steamworks initialization failed! Result: k_ESteamAPIInitResult_FailedGeneric, message: No appID "
        "found.\n" + "x" * 700_000 + "\nERROR: ... ERR_CANT_CREATE\n at: swap_chain_resize "
        "(drivers/d3d12/rendering_device_driver_d3d12.cpp:2854)\n")
    by_key = {s["key"]: s for s in library.suggestions(e.id)}
    assert by_key["options-command"]["changes"] == {
        "launch_args": "systemctl --user restart pipewire wireplumber; %command%"}
    assert by_key["dll-overrides"]["changes"] == {"enable_fix": "game_dlls"}
    assert "No appID found" in by_key["dll-overrides"]["detail"]
    assert "swap-chain errors" in by_key["godot-vulkan"]["detail"]
    library.apply_suggestion(e.id, "options-command")
    library.apply_suggestion(e.id, "dll-overrides")
    library.apply_suggestion(e.id, "godot-vulkan")
    e = library.get(e.id)
    assert set(e.compat_config.fixes) == {"game_dlls", "godot_vulkan"}
    assert e.launch_args == "systemctl --user restart pipewire wireplumber; %command%"
    assert library.suggestions(e.id) == []                      # everything resolved
    plan = library.launch_plan(e.id)
    assert plan.env["WINEDLLOVERRIDES"] == "winmm=n,b"
    assert plan.shell_script.endswith("SlayTheSpire2.exe' --rendering-driver vulkan") or \
        plan.shell_script.endswith("SlayTheSpire2.exe --rendering-driver vulkan")
    from ezgd.library.manager import LibraryError
    with pytest.raises(LibraryError):
        library.apply_suggestion(e.id, "godot-vulkan")


def test_with_game_args_respects_command():
    from ezgd.library.advisor import merged_overrides, with_game_args
    assert with_game_args("", "--x") == "--x"
    assert with_game_args("-windowed", "--x") == "-windowed --x"
    assert with_game_args("A=1 %command% -w", "--x") == "A=1 %command% --x -w"
    assert merged_overrides("dinput8=n,b", ["winmm", "dinput8"]) == "dinput8=n,b;winmm=n,b"


# ------------------------------------------------------------ Fixes screen (checkbox catalog)
def test_fix_catalog_for_onlinefix_godot_game(library, tmp_path):
    e, exe = _godot_onlinefix_game(tmp_path, library)
    library.set_executable(e.id, exe)
    library.set_fixes(e.id, ["protonfixes", "runtime"])           # the defaults only
    logs = library.prefix_dir(e.id) / "pfx/drive_c/users/steamuser/AppData/Roaming/SlayTheSpire2/logs"
    logs.mkdir(parents=True)
    (logs / "godot.log").write_text("message: No appID found\nrendering_device_driver_d3d12 ERR_CANT_CREATE\n")
    items = {i["key"]: i for i in library.fixes_for(e.id)}
    assert items["game_dlls"]["recommended"] and "OnlineFix" in items["game_dlls"]["recommended"]
    assert "No appID found" in items["game_dlls"]["recommended"]
    assert "swap-chain" in items["godot_vulkan"]["recommended"]
    assert not items["godot_opengl"]["recommended"] and items["godot_opengl"]["group"] == "godot_renderer"
    assert {"opt:wined3d", "opt:no_fsync", "protonfixes", "runtime", "pulse_latency", "gamemode"} <= set(items)
    assert items["protonfixes"]["enabled"] and items["runtime"]["enabled"]
    assert not any(i["enabled"] for k, i in items.items() if k not in ("protonfixes", "runtime"))


def test_switching_fixes_on_and_off(library, tmp_path, monkeypatch):
    e, exe = _godot_onlinefix_game(tmp_path, library)
    library.set_executable(e.id, exe)
    library.update(e.id, launch_args="-windowed")
    e = library.set_fixes(e.id, ["game_dlls", "godot_vulkan", "godot_opengl", "opt:no_fsync", "pulse_latency"])
    cfg = e.compat_config
    assert set(cfg.fixes) == {"game_dlls", "godot_vulkan", "pulse_latency"}   # one renderer only
    assert cfg.options == ["no_fsync"]
    assert not cfg.protonfixes and not cfg.use_runtime            # unticked on the same screen
    plan = library.launch_plan(e.id)
    assert plan.env["PULSE_LATENCY_MSEC"] == "60" and plan.env["PROTON_NO_FSYNC"] == "1"
    assert plan.env["WINEDLLOVERRIDES"] == "winmm=n,b"
    assert plan.shell_script.endswith("--rendering-driver vulkan -windowed")    # fix args, then user options
    assert "fixes: game DLLs, Vulkan, audio buffer" in plan.description
    # untick everything except protonfixes: the fixes are gone again, user options stay
    library.update(e.id, compat={**library.get(e.id).compat_config.to_dict(), "dll_overrides": "winmm=n,b"})
    e = library.set_fixes(e.id, ["protonfixes"])
    plan = library.launch_plan(e.id)
    assert "WINEDLLOVERRIDES" not in plan.env and "--rendering-driver" not in plan.shell_script
    assert e.compat_config.dll_overrides == ""                    # manual copy of the same DLL removed too
    assert not e.compat_config.use_runtime and e.compat_config.protonfixes
    assert plan.shell_script.endswith("-windowed")


def test_tool_fixes_need_the_tool(library, tmp_path, monkeypatch):
    e, exe = _godot_onlinefix_game(tmp_path, library)
    library.set_executable(e.id, exe)
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None if n == "mangohud" else real(n, *a, **k))
    items = {i["key"]: i for i in library.fixes_for(e.id)}
    assert not items["mangohud"]["available"] and "System check" in items["mangohud"]["unavailable_reason"]


@pytest.mark.proton
def test_real_proton_runs_release_cmd_script(library, tmp_path):
    """Releases like Black Ops II start each mode through a .cmd that first swaps steam_api.dll:
    the script must run inside the game folder (relative paths) and then start the game."""
    exe_src = Path("/usr/lib/wine/x86_64-windows/cmd.exe")
    if not exe_src.exists() or not library.protons():
        pytest.skip("needs Proton and Wine's cmd.exe")
    folder = tmp_path / "games" / "Release"
    folder.mkdir(parents=True)
    shutil.copy(exe_src, folder / "game.exe")
    (folder / "steam_api.dll").write_bytes(b"single-player")
    (folder / "steam_api.dll_mp").write_bytes(b"multiplayer")
    (folder / "Plugins").mkdir()
    (folder / "Plugins" / "ZonePlugin.Red32n").write_bytes(b"p")
    (folder / "mp.cmd").write_bytes(b'@echo off\r\ncopy steam_api.dll_mp steam_api.dll\r\n'
                                    b'if exist "Plugins\\ZonePlugin.Red32n" move "Plugins\\ZonePlugin.Red32n" '
                                    b'"Plugins\\ZonePlugin.Red32n.old"\r\n"game.exe" /c "echo started> started.txt"\r\n')
    e = library.import_folder("Release", folder)
    library.set_executable(e.id, folder / "mp.cmd")
    library.launch(e.id, confirmed=True)
    deadline = time.time() + 300
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.5)
    library.reap()
    log_tail = (library.paths.logs / f"game-{e.id}.log").read_text(errors="replace")[-2000:]
    assert (folder / "steam_api.dll").read_bytes() == b"multiplayer", log_tail
    assert (folder / "Plugins" / "ZonePlugin.Red32n.old").exists(), log_tail
    assert (folder / "started.txt").exists(), log_tail


def test_advisor_suggests_the_release_launch_script_for_black_ops_ii(library, tmp_path):
    """The Black Ops II release starts each mode through mp.cmd / zm.cmd / sp.cmd, which swap steam_api.dll first;
    started directly, the multiplayer exe loads the single-player (real Steam) DLL and restarts via Steam."""
    folder = tmp_path / "games" / "Call of Duty Black Ops II"
    folder.mkdir(parents=True)
    mz = b"MZ" + b"\0" * 64
    for name in ("t6mp - Multiplayer Offline.exe", "t6zm - Zombies Offline.exe", "t6sp.exe"):
        (folder / name).write_bytes(mz)
    (folder / "mp.cmd").write_text('@echo off\r\ncopy steam_api.dll_zmp steam_api.dll\r\nif exist '
                                   '"Plugins\\ZonePlugin.Red32n" move "Plugins\\ZonePlugin.Red32n" '
                                   '"Plugins\\ZonePlugin.Red32n.old"\r\n"t6mp - Multiplayer Offline.exe"')
    (folder / "zm.cmd").write_text('@echo off\r\ncopy steam_api.dll_zmp steam_api.dll\r\n"t6zm - Zombies Offline.exe"')
    (folder / "sp.cmd").write_text("@echo off\r\ncopy steam_api.dll_sp steam_api.dll\r\nt6sp.exe")
    (folder / "notes.bat").write_text("echo t6mp - Multiplayer Offline.exe is the multiplayer\r\n")  # no preparation
    (folder / "t6mp - Multiplayer Offline.exe.304.STEAMSTART").write_bytes(b"\0")
    e = library.import_folder("Call of Duty: Black Ops II", folder)
    library.set_executable(e.id, folder / "t6mp - Multiplayer Offline.exe")
    s = next(s for s in library.suggestions(e.id) if s["key"] == "launch-script")
    assert s["changes"] == {"set_executable": str(folder / "mp.cmd")} and s["severity"] == "warning"
    assert "copy steam_api.dll_zmp steam_api.dll" in s["detail"] and ".STEAMSTART" in s["detail"]
    library.apply_suggestion(e.id, "launch-script")
    e = library.get(e.id)
    assert e.executable == str(folder / "mp.cmd") and not e.executable_trusted   # confirmed again on Play
    assert not [s for s in library.suggestions(e.id) if s["key"] == "launch-script"]
    plan = library.launch_plan(e.id)
    assert plan.cwd == str(folder)                      # the script's relative paths resolve in the game folder
    library.set_executable(e.id, folder / "t6sp.exe")
    s = next(s for s in library.suggestions(e.id) if s["key"] == "launch-script")
    assert s["changes"] == {"set_executable": str(folder / "sp.cmd")} and s["severity"] == "info"
    names = [c.path.name for c in library.candidates(e.id)]
    assert names[0] == "sp.cmd" and names.index("sp.cmd") < names.index("t6sp.exe")   # release scripts first


@pytest.mark.proton
def test_real_proton_diagnostic_launch_is_analysed(library, tmp_path):
    exe_src = Path("/usr/lib/wine/x86_64-windows/cmd.exe")
    if not exe_src.exists() or not library.protons():
        pytest.skip("needs Proton and Wine's cmd.exe")
    folder = tmp_path / "games" / "Diag"
    folder.mkdir(parents=True)
    shutil.copy(exe_src, folder / "cmd.exe")
    e = library.import_folder("Diag", folder)
    library.set_executable(e.id, folder / "cmd.exe")
    library.update(e.id, launch_args="%command% /c exit 3")
    library.launch(e.id, confirmed=True, diagnostic=True)
    deadline = time.time() + 300
    while library.is_running(e.id) and time.time() < deadline:
        time.sleep(0.5)
    library.reap()
    from ezgd.library.diagnose import last_run
    run = last_run(library.paths.logs / f"game-{e.id}.log")
    assert run.diagnostic and run.exit_code is not None and run.seconds is not None
    assert "trace:loaddll" in run.text                       # Wine's detailed logging really was on
    d = library.diagnose(e.id)
    assert d["run"]["diagnostic"] is True and "exited with code" in d["summary"]


def test_trial_candidates(library, tmp_path):
    folder = tmp_path / "games" / "T"
    folder.mkdir(parents=True)
    (folder / "T-Win64-Shipping.exe").write_bytes(b"MZ" + b"\0" * 64)
    e = library.import_folder("T", folder)
    library.set_executable(e.id, folder / "T-Win64-Shipping.exe")
    findings = [{"key": "dx12-unreal", "fix": {"kind": "add_args", "value": "-dx11", "label": "Start with DirectX 11"}},
                {"key": "graphics-crash", "fix": {"kind": "enable_fix", "value": "opt:wined3d",
                                                  "label": "Use WineD3D instead of DXVK"}}]
    cands = library.trial_candidates(e.id, findings)
    labels = [c["label"] for c in cands]
    assert labels[0].startswith("Current setup") and cands[0]["overrides"] == {}
    assert "Start with DirectX 11" in labels and labels.count("Use WineD3D instead of DXVK") == 1
    assert len(cands) <= library.MAX_TRIALS
    assert any(c["overrides"].get("proton_version") for c in cands) == (len(library.protons()) > 1)


@pytest.mark.proton
def test_real_proton_trials(library, tmp_path, monkeypatch):
    exe_src = Path("/usr/lib/wine/x86_64-windows/cmd.exe")
    if not exe_src.exists() or not library.protons():
        pytest.skip("needs Proton and Wine's cmd.exe")
    monkeypatch.setattr(library, "TRIAL_SECONDS", 20)
    folder = tmp_path / "games" / "Trial"
    folder.mkdir(parents=True)
    shutil.copy(exe_src, folder / "cmd.exe")
    e = library.import_folder("Trial", folder)
    library.set_executable(e.id, folder / "cmd.exe")
    real_prefix = library.prefix_dir(e.id)
    keeps = {"label": "keeps running", "overrides": {"launch_args": "%command% /c ping -n 60 127.0.0.1"}}
    quits = {"label": "quits", "overrides": {"launch_args": "%command% /c exit 3"}}
    r1 = library.run_trial(e.id, keeps, 1)
    r2 = library.run_trial(e.id, quits, 2)
    assert r1["outcome"] == "ran" and "kept running" in r1["detail"], r1
    assert r2["outcome"] == "exited" and "code" in r2["detail"], r2
    assert not list(library.paths.prefixes.glob(f"trial-{e.id}-*"))          # test prefixes removed
    assert not real_prefix.exists() or not any(real_prefix.iterdir())       # the real prefix was never used
    assert "trial result: ran" in (library.paths.logs / f"trial-{e.id}.log").read_text()


def test_frame_detection():
    from ezgd.library.manager import _drew_frames
    assert _drew_frames("info:  DXVK: v3.0\ninfo:  Presenter: Actual swap chain properties:\n") is True
    assert _drew_frames("info:  DXVK: v3.0\ninfo:  Found device: NVIDIA\n") is False
    assert _drew_frames("nothing graphical here") is None


@pytest.mark.proton
def test_real_proton_switch_gets_its_own_prefix(library, tmp_path):
    """Grounding for ezgd.platform.prefixguard: a prefix made by Valve Proton is not handed to GE/CachyOS (whose
    upgrade fails); the second Proton gets its own prefix, saves come along, and the game still runs."""
    from ezgd.platform import prefixguard as pg
    exe_src = Path("/usr/lib/wine/x86_64-windows/cmd.exe")
    ps = library.protons()
    fams = {}
    for p in ps:
        fams.setdefault(pg.family(pg.proton_prefix_version(p.path)), p)
    if not exe_src.exists() or len(fams) < 2:
        pytest.skip("needs Wine's cmd.exe and two kinds of Proton (Valve / GE / CachyOS)")
    (first_fam, valve), (_f2, other) = list(fams.items())[:2]
    folder = tmp_path / "games" / "Cmd2"
    folder.mkdir(parents=True)
    exe = folder / "cmd.exe"
    shutil.copy(exe_src, exe)
    e = library.import_folder("Cmd2", folder)
    library.set_executable(e.id, exe)

    def run(proton_name: str, marker: str) -> None:
        library.update(e.id, proton_version=proton_name,
                       launch_args=rf"%command% /c 'echo {marker}> C:\users\steamuser\AppData\Roaming\{marker}.txt'")
        library.launch(e.id, confirmed=True)
        deadline = time.time() + 300
        while library.is_running(e.id) and time.time() < deadline:
            time.sleep(0.5)
        library.reap()
    run(valve.name, "first")
    pfx = library.prefix_dir(e.id)
    save = pfx / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "first.txt"
    assert save.exists() and pg.family(pg.prefix_version(pfx)) == first_fam
    run(other.name, "second")
    assert (pfx.parent / pg.slot_name(pfx.name, first_fam)).is_dir()        # the first prefix kept for switching back
    assert save.exists()                                                     # the save came along
    assert (save.parent / "second.txt").exists()                             # and the game ran in the new prefix
    log = (library.paths.logs / f"game-{e.id}.log").read_text(errors="replace")
    assert "invalid version" not in log.rsplit("prefix switched", 1)[-1]     # no mixed-up prefix this time
