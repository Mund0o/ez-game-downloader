"""The frame meter: every test run logs its frames with an invisible MangoHud, because a screenshot can be black while
the game draws (Ghost Recon Wildlands' hidden window always captured black — and Fix my game once chose the wrong
Proton on such a picture). Frames counted beat a black picture; frames that stop while the game still runs are a
freeze; a Vulkan game that presented nothing drew nothing; MangoHud crashing in a game is never blamed on the setup."""

import shutil
import time
from pathlib import Path

from ezgd.library import manager as manager_mod
from ezgd.library import probes
from ezgd.library.manager import _with_meter

from test_release_fixes import core  # noqa: F401 — the shared AppCore fixture

FIXTURES = Path(__file__).parent / "fixtures" / "mangohud"


def test_a_real_mangohud_log_is_read_as_a_timeline(tmp_path):
    """(Recorded from vkcube with MangoHud 0.8.4: one row per frame, ``elapsed`` in nanoseconds since the log started
    — every 8th row kept.)"""
    shutil.copy(FIXTURES / "vkcube_2026-10-27_17-51-28.csv", tmp_path)
    began = time.mktime(time.strptime("2026-10-27_17-51-28", "%Y-%m-%d_%H-%M-%S"))
    m = probes.read_frames(tmp_path, began - 10)          # the run started 10 s before the log
    assert m["frames"] == 45 and m["process"] == "vkcube"
    assert 10 <= m["first"] < 10.1 and 12.9 < m["last"] < 13.1 and m["gap"] <= 0.1
    # the log of another process with fewer frames (a launcher) is not the game's
    (tmp_path / "launcher_2026-10-27_17-51-28.csv").write_text("fps,frametime,elapsed\n60,16.6,1000\n")
    assert probes.read_frames(tmp_path, began - 10)["process"] == "vkcube"
    assert probes.read_frames(tmp_path / "none", began) is None


def test_what_the_frames_say():
    drew = {"frames": 400, "first": 6.0, "last": 44.0, "gap": 0.2}
    assert probes.frame_verdict(drew, 45, True, True) == {"drawing": True, "stalled": 0,
                                                          "text": "it drew 400 frames (from 6 s on, MangoHud)"}
    stopped = probes.frame_verdict({**drew, "last": 20.0}, 45, True, True)
    assert stopped["drawing"] and stopped["stalled"] == 25 and "then none for the last 25 s" in stopped["text"]
    assert probes.frame_verdict({**drew, "last": 20.0}, 45, False, True)["stalled"] == 0     # it crashed: no freeze
    none = {"frames": 2, "first": 30.0, "last": 30.1, "gap": 0}
    assert probes.frame_verdict(none, 45, True, True)["drawing"] is False
    assert probes.frame_verdict(none, 45, True, False)["drawing"] is None    # OpenGL / WineD3D: MangoHud is blind
    assert probes.frame_verdict(None, 45, True, True)["drawing"] is None     # no log: says nothing


def test_frames_correct_the_picture():
    drawing = probes.frame_verdict({"frames": 400, "first": 6.0, "last": 44.0, "gap": 0}, 45, True, True)
    shown, why, frames = _with_meter(drawing, "black", "its picture stayed black (3 looks)", False)
    assert shown == "unknown" and frames is True and why.startswith("its window was captured black, but it drew")
    assert _with_meter(drawing, "menu", "it showed its menu", True)[0] == "menu"
    frozen = probes.frame_verdict({"frames": 400, "first": 6.0, "last": 12.0, "gap": 0}, 45, True, True)
    assert _with_meter(frozen, "unknown", "", None)[0] == "frozen"
    black = probes.frame_verdict({"frames": 0, "first": None, "last": None, "gap": 0}, 45, True, True)
    assert _with_meter(black, "unknown", "", None)[2] is False
    assert _with_meter(black, "unknown", "", True)[2] is True     # DXVK saw a swap chain present: that stands


GAME = """#!/bin/sh
# a test "game": presents FRAMES frames over its first 2 seconds (MangoHud's log, where the frame meter's config says),
# says it loaded DXGI like a Wine log would, then runs on
out=$(sed -n 's/^output_folder=//p' "$MANGOHUD_CONFIGFILE")
echo "MANGOHUD=$MANGOHUD" >> "{seen}"
echo '0134:trace:loaddll:build_module Loaded L"C:\\\\\\\\windows\\\\\\\\system32\\\\\\\\dxgi.dll" at 1: native'
if [ -n "$out" ]; then
  f="$out/game_$(date +%Y-%m-%d_%H-%M-%S).csv"
  {{ echo "os,cpu,gpu"; echo "Linux,cpu,gpu"; echo "fps,frametime,elapsed"; }} > "$f"
  i=0
  while [ $i -lt {frames} ]; do echo "100,10,$((i * 10000000))" >> "$f"; i=$((i + 1)); done
fi
sleep 60
"""


def game(core, tmp_path, frames: int):  # noqa: F811
    folder = tmp_path / "games" / "F"
    folder.mkdir(parents=True, exist_ok=True)
    seen = tmp_path / "seen.txt"
    script = folder / "run.sh"
    script.write_text(GAME.format(frames=frames, seen=seen))
    script.chmod(0o755)
    e = core.library.import_folder("F", folder)
    core.library.set_executable(e.id, script)
    return e, seen


def with_mangohud(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(manager_mod.shutil, "which", lambda t, *a, **k: "/usr/bin/mangohud" if t == "mangohud"
                        else real(t, *a, **k))


def test_every_test_counts_its_frames(core, tmp_path, monkeypatch):  # noqa: F811
    with_mangohud(monkeypatch)
    e, _seen = game(core, tmp_path, 200)
    monkeypatch.setattr(core.library, "TRIAL_SECONDS", 6)
    monkeypatch.setattr(core.library, "SCREEN_LOOKS", ())
    r = core.library.run_trial(e.id, {"label": "t", "overrides": {}}, 1)
    assert r["outcome"] == "ran" and r["frames"] is True and r["meter"]["frames"] == 200, r
    assert "it drew 200 frames" in r["detail"] and "drawing frames" in r["progress"]
    assert not list((core.paths.data / "measure").glob("frames-*/*.csv"))           # the logs are not kept
    # frames that stop while it runs: frozen
    monkeypatch.setattr(probes, "STALL_SECONDS", 3)
    r = core.library.run_trial(e.id, {"label": "t", "overrides": {}}, 2)
    assert r["picture"] == "frozen" and r["stuck"] and "then none for the last" in r["detail"], r


def test_a_vulkan_game_that_presents_nothing_drew_nothing(core, tmp_path, monkeypatch):  # noqa: F811
    with_mangohud(monkeypatch)
    e, _seen = game(core, tmp_path, 0)
    monkeypatch.setattr(core.library, "TRIAL_SECONDS", 4)
    monkeypatch.setattr(core.library, "SCREEN_LOOKS", ())
    r = core.library.run_trial(e.id, {"label": "t", "overrides": {}}, 1)
    assert r["outcome"] == "ran" and r["frames"] is False and r["stuck"], r


def test_mangohud_crashing_is_never_blamed_on_the_setup(core, tmp_path, monkeypatch):  # noqa: F811
    from ezgd.library import syslog
    with_mangohud(monkeypatch)
    e, seen = game(core, tmp_path, 200)
    (Path(core.library.get(e.id).executable)).write_text("#!/bin/sh\necho \"MANGOHUD=$MANGOHUD\" >> " + str(seen)
                                                         + "\nexit 139\n")
    monkeypatch.setattr(core.library, "TRIAL_SECONDS", 4)
    monkeypatch.setattr(core.library, "SCREEN_LOOKS", ())
    calls = []

    def system_log(entry_id, since, pids, exe=None):
        calls.append(1)
        if len(calls) == 1:                                   # the first run: a segfault inside MangoHud
            ev = syslog.Event("segfault", "run.sh", 1, time.time(), detail="segfault at 0", lib="libMangoHud.so")
            return [ev], ""
        return [], ""
    monkeypatch.setattr(core.library, "_system_log", system_log)
    r = core.library.run_trial(e.id, {"label": "t", "overrides": {}}, 1)
    assert seen.read_text().split() == ["MANGOHUD=1", "MANGOHUD="]          # tested again, without MangoHud
    assert "meter_crash" not in r and e.id in core.library._no_meter
