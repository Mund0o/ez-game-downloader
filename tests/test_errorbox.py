"""Error pop-ups a game shows: their text comes from Wine's msgbox channel, the assistant acts on it."""

import shutil
import time
from pathlib import Path

import pytest

from ezgd.library import diagnose as dg
from ezgd.library import errorbox
from ezgd.library.models import LibraryEntry, LibraryStatus

# what Wine writes (seen 2026-10-14 with GE-Proton11-5: WINEDEBUG=+msgbox, a WScript.Echo message box)
@pytest.fixture
def library(db, settings, app_paths):
    from ezgd.database.repositories import GameRepository, LibraryRepository
    from ezgd.library.manager import LibraryManager
    return LibraryManager(LibraryRepository(db), GameRepository(db), settings, app_paths, lambda n, p: None)


BOX = '0204:trace:msgbox:MSGBOX_OnInit L"Fatal error!\\n\\nVCRUNTIME140_1.dll was not found. Reinstalling may fix this."\n'


def test_messages_are_read_from_wines_log():
    text = "noise\n" + BOX + BOX + '0300:trace:msgbox:MSGBOX_OnInit L"caf\\x00e9 \\"quoted\\""\n'
    assert errorbox.messages(text) == ["Fatal error! VCRUNTIME140_1.dll was not found. Reinstalling may fix this.",
                                       'café "quoted"']
    assert errorbox.shown(BOX) and not errorbox.shown("trace:loaddll")
    assert errorbox.explain("Failed to initialize Steam")[0] == "steam"
    assert errorbox.explain("DirectX 12 is not supported on your system")[2][1] == "-dx11"


def test_the_assistant_reads_the_error_box(tmp_path):
    g = tmp_path / "Game"
    g.mkdir()
    (g / "Game.exe").write_bytes(b"MZ")
    log = tmp_path / "game-1.log"
    log.write_text("\n=== 2026-10-14 10:00:00 GE-Proton · diagnostic run\n" + BOX + "\n=== exited with code 1 after 2s\n")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"),
                     install_path=str(g))
    r = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()
    top = r["findings"][0]
    assert top["key"] == "error-box:vcrun" and "VCRUNTIME140_1.dll was not found" in top["explanation"]
    assert top["fix"] == {"kind": "winetricks", "value": "vcrun2022", "label": "Install the Visual C++ runtimes"}


def test_trial_reason_quotes_the_error_box():
    from ezgd.library.manager import _trial_reason
    assert _trial_reason(BOX).startswith("the game showed an error: “Fatal error!")


def test_play_launches_log_error_boxes(library, tmp_path):
    folder = tmp_path / "games" / "B"
    folder.mkdir(parents=True)
    (folder / "B.exe").write_bytes(b"MZ" + b"\0" * 64)
    e = library.import_folder("B", folder)
    library.set_executable(e.id, folder / "B.exe")
    plan = library.launch_plan(e.id)
    if plan.env.get("PROTON_LOG") or not library.protons():
        pytest.skip("needs a Proton")
    assert "+msgbox" in plan.env.get("WINEDEBUG", "")
    assert "+msgbox" in library.DIAGNOSTIC_WINEDEBUG


@pytest.mark.proton
def test_real_proton_error_box_ends_a_trial_at_once(library, tmp_path, monkeypatch):
    """A real message box under Proton: the trial reads its text and closes the game right away (not after the
    trial's full time)."""
    src = Path("/usr/lib/wine/x86_64-windows/wscript.exe")
    if not src.exists() or not library.protons():
        pytest.skip("needs Proton and Wine's wscript.exe")
    monkeypatch.setattr(library, "TRIAL_SECONDS", 120)
    folder = tmp_path / "games" / "Box"
    folder.mkdir(parents=True)
    shutil.copy(src, folder / "wscript.exe")
    script = folder / "err.js"
    script.write_text('WScript.Echo("Fatal error: VCRUNTIME140_1.dll was not found.");')
    e = library.import_folder("Box", folder)
    library.set_executable(e.id, folder / "wscript.exe")
    started = time.monotonic()
    r = library.run_trial(e.id, {"label": "box", "overrides": {"launch_args": f"%command% 'Z:{script}'"}}, 1)
    assert r["outcome"] == "crashed" and "VCRUNTIME140_1.dll was not found" in r["detail"], r
    assert time.monotonic() - started < 90


NOTICE = ('014c:trace:msgbox:MSGBOX_OnInit L"The fix is made by online-fix.me. Please notice that the only trusted '
          'sites are online-fix.me and cs.rin.ru. You will get this message only once. Enjoy :)"\n')


def test_onlinefixs_one_time_notice_is_not_an_error():
    """Expedition 33's test with the OnlineFix DLLs loaded stopped on this greeting and was counted as a crash."""
    from ezgd.library.manager import _trial_reason
    assert errorbox.messages(NOTICE) == [] and not errorbox.shown(NOTICE) and errorbox.notice_shown(NOTICE)
    assert errorbox.messages(NOTICE, notices=True)[0].startswith("The fix is made by online-fix.me")
    assert errorbox.shown(NOTICE + BOX) and errorbox.messages(NOTICE + BOX) == [
        "Fatal error! VCRUNTIME140_1.dll was not found. Reinstalling may fix this."]
    assert _trial_reason(NOTICE) == ""


def test_a_trial_held_by_a_notice_runs_again(library, tmp_path, monkeypatch):
    """The notice waits for OK, but comes only once: the test starts the game again and judges that start."""
    import subprocess
    from ezgd.platform import launcher
    folder = tmp_path / "games" / "N"
    folder.mkdir(parents=True)
    (folder / "N.exe").write_bytes(b"MZ" + b"\0" * 64)
    e = library.import_folder("N", folder)
    library.set_executable(e.id, folder / "N.exe")
    monkeypatch.setattr(library, "_trial_prefix", lambda eid, n, p=None: (tmp_path / "copy", "a copy"))
    monkeypatch.setattr(library, "launch_plan", lambda eid, o=None: launcher.LaunchPlan(["sleep", "30"], str(folder)))
    monkeypatch.setattr(library, "TRIAL_SECONDS", 3)
    monkeypatch.setattr(library, "SCREEN_LOOKS", ())
    starts = []

    def spawn(plan, log_path):
        starts.append(1)
        if len(starts) == 1:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(NOTICE)                          # first start: OnlineFix greets
        return subprocess.Popen(plan.argv, start_new_session=True)

    monkeypatch.setattr(library, "_spawn", spawn)
    r = library.run_trial(e.id, {"label": "game DLLs", "overrides": {}}, 1)
    assert len(starts) == 2 and r["outcome"] == "ran", r
    assert "notice" not in r["detail"]


# --------------------------------------------------------- errors drawn by the game itself (screenshots)
def test_screen_text_is_filtered_to_errors():
    from ezgd.library import screenread
    ocr = "Valor Mortis\nNew Game\n|| ~~ ;;\nFatal error: Failed to create D3D12 device\nOptions\nUnable to load save data\n"
    lines = screenread.error_lines(ocr)
    assert lines == ["Fatal error: Failed to create D3D12 device", "Unable to load save data"]
    assert screenread.stopped_on_error(lines) and not screenread.stopped_on_error(["Graphics: DirectX 12"])
    log = "x\n" + "".join(screenread.PREFIX + ln + "\n" for ln in lines)
    assert screenread.from_log(log) == lines


def test_the_assistant_reads_the_screen(tmp_path):
    from ezgd.library import screenread
    g = tmp_path / "Game"
    g.mkdir()
    (g / "Game.exe").write_bytes(b"MZ")
    log = tmp_path / "game-1.log"
    log.write_text("\n=== 2026-10-14 10:00:00 GE-Proton · diagnostic run\n"
                   f"{screenread.PREFIX}Fatal error: DirectX 12 is not supported on your system\n"
                   "=== exited with code 1 after 9s\n")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"),
                     install_path=str(g))
    top = dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="GE").to_dict()["findings"][0]
    assert top["key"] == "screen-error:dx12" and top["fix"]["value"] == "-dx11"
    from ezgd.library.manager import _trial_reason
    assert "screen showed" in _trial_reason(log.read_text())


def test_a_trial_ends_when_the_screen_shows_an_error(library, tmp_path, monkeypatch):
    from ezgd.library import screenread
    folder = tmp_path / "games" / "S"
    folder.mkdir(parents=True)
    script = folder / "run.sh"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    e = library.import_folder("S", folder)
    library.set_executable(e.id, script)
    monkeypatch.setattr(library, "SCREEN_LOOKS", (1,))
    monkeypatch.setattr(library, "TRIAL_SECONDS", 120)         # it must end long before this

    def look(path):
        with open(path, "a") as fh:
            fh.write(screenread.PREFIX + "Fatal error: the game crashed\n")
        return ["Fatal error: the game crashed"]
    monkeypatch.setattr(screenread, "look", look)
    started = time.monotonic()
    r = library.run_trial(e.id, {"label": "s", "overrides": {}}, 1)
    assert r["outcome"] == "crashed" and "the game crashed" in r["detail"], r
    assert time.monotonic() - started < 60                     # (a busy computer: still far below 120 s)


# ---------------------------------------------------------------- editing an Unreal game's settings
def test_ini_edits_keep_everything_else():
    from ezgd.library.fixgen import set_ini
    text = "[/Script/Engine.GameUserSettings]\nbUseVSync=False\nFullscreenMode=0\n\n[Other]\na=1\n"
    out = set_ini(text, "/Script/Engine.GameUserSettings", "FullscreenMode", "2")
    assert "FullscreenMode=2" in out and "bUseVSync=False" in out and "[Other]\na=1" in out
    out = set_ini(out, "ScalabilityGroups", "sg.TextureQuality", "1")
    assert out.rstrip().endswith("[ScalabilityGroups]\nsg.TextureQuality=1")
    assert set_ini("", "S", "k", "v") == "[S]\nk=v\n"


def test_unreal_settings_fixes_are_generated_tested_and_undone(tmp_path):
    from ezgd.library import fixgen
    root = tmp_path / "Game"
    ship = root / "Valor" / "Binaries" / "Win64" / "Valor-Win64-Shipping.exe"
    ship.parent.mkdir(parents=True)
    ship.write_bytes(b"MZ")
    (root / "Engine").mkdir()
    (root / "Valor.exe").write_bytes(b"MZ")
    e = LibraryEntry(title="Valor", status=LibraryStatus.INSTALLED, id=1, executable=str(root / "Valor.exe"),
                     install_path=str(root))
    run = dg.LastRun(started="x", text="")
    c = dg.Context(e, root / "Valor.exe", tmp_path / "pfx", run, "Unreal Engine", "", "", False, [], "GE", True,
                   True, {})
    fixes = {f.gen_id: f for f in fixgen.generate(c, [], "black_screen")}
    assert {"ue:config-dx11", "ue:config-windowed", "ue:nomovie"} <= set(fixes)
    cfg = tmp_path / "pfx" / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Valor" / "Saved" / \
        "Config" / "Windows"
    cfg.mkdir(parents=True)                           # the game has run once: its settings folder exists
    (cfg / "Engine.ini").write_text("[Core.System]\nPaths=../../../Engine/Content\n")
    fixes = {f.gen_id: f for f in fixgen.generate(c, [], "black_screen")}
    done = fixgen.apply_edits(fixes["ue:config-dx11"].edits)
    text = (cfg / "Engine.ini").read_text()
    assert "Paths=../../../Engine/Content" in text and \
        "[/Script/WindowsTargetPlatform.WindowsTargetSettings]\nDefaultGraphicsRHI=DefaultGraphicsRHI_DX11" in text
    done += fixgen.apply_edits(fixes["ue:config-windowed"].edits)
    assert "FullscreenMode=2" in (cfg / "GameUserSettings.ini").read_text()
    fixgen.revert_edits(done)                         # a test run is undone completely
    assert (cfg / "Engine.ini").read_text() == "[Core.System]\nPaths=../../../Engine/Content\n"
    assert not (cfg / "GameUserSettings.ini").exists()


@pytest.mark.proton
def test_real_proton_prepares_a_prefix_before_the_game_ever_ran(library, tmp_path):
    """Switching a game to another Proton gives it a new, empty prefix: runtimes must go in before the first start."""
    from ezgd.platform.proton import prefix_ready
    src = Path("/usr/lib/wine/x86_64-windows/cmd.exe")
    if not src.exists() or not library.protons():
        pytest.skip("needs Proton")
    folder = tmp_path / "games" / "P"
    folder.mkdir(parents=True)
    shutil.copy(src, folder / "game.exe")
    e = library.import_folder("P", folder)
    library.set_executable(e.id, folder / "game.exe")
    pfx = tmp_path / "newprefix"
    assert library.init_prefix(e.id, pfx, library.protons()[0].name)
    assert prefix_ready(pfx)


def test_the_games_own_words_beat_the_install_list(tmp_path):
    """Visual C++ was listed as installed in the prefix, so its fix was dropped ("already installed") — while the
    game said it was missing. The game wins: it is installed again (forced)."""
    g = tmp_path / "Game"
    g.mkdir()
    (g / "Game.exe").write_bytes(b"MZ")
    pfx = tmp_path / "pfx"
    (pfx / "pfx").mkdir(parents=True)
    (pfx / "pfx" / "winetricks.log").write_text("vcrun2022\n")
    log = tmp_path / "game-1.log"
    log.write_text("\n=== 2026-10-14 10:00:00 GE-Proton\n" + BOX + "=== exited with code 41 after 2s\n")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "Game.exe"),
                     install_path=str(g))
    top = dg.diagnose(e, pfx, log, protons=[], proton_name="GE").to_dict()["findings"][0]
    assert top["fix"]["value"] == "--force vcrun2022" and top["confidence"] >= 0.9


def test_the_screen_is_only_read_when_a_windows_program_is_in_front(monkeypatch):
    import subprocess
    from ezgd.library import screenread

    class R:
        def __init__(self, out):
            self.stdout, self.returncode = out, 0
    outs = {"-root": "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x6e00001",
            "-id": 'WM_CLASS(STRING) = "code", "Code"\n_NET_WM_PID(CARDINAL) = 1'}
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(screenread.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: R(outs[cmd[1]]))
    assert not screenread.game_window_active()                  # an editor in front: never photographed
    outs["-id"] = 'WM_CLASS(STRING) = "steam_proton", "steam_proton"\n'
    assert screenread.game_window_active()                      # Proton's game window
