"""Test runs that watch for the reported problem (ezgd.library.probes): no sound, a controller the game does not see
(or sees twice), a game that runs badly — the verdict is what the run showed, not "it kept running"."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ezgd.library import diagnose as dg
from ezgd.library import fixgen, probes
from ezgd.library.manager import LibraryManager
from ezgd.library.models import LibraryEntry, LibraryStatus
from pe_builder import build_pe

from test_release_fixes import core, install  # noqa: F401 — the shared AppCore fixture

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PipeWire, /sys, /proc")

GAME = {1001, 1002}
HEADSET = {"index": 7, "name": "alsa_output.usb-headset", "description": "Headset", "mute": False}
HDMI = {"index": 9, "name": "alsa_output.hdmi", "description": "TV (HDMI)", "mute": False}


def stream(sink=7, corked=False, mute=False, volume="100%", pid=1001):
    return {"index": 50, "sink": sink, "corked": corked, "mute": mute,
            "volume": {"front-left": {"value_percent": volume}}, "properties": {"application.process.id": str(pid)}}


def sound(monkeypatch, streams, sinks=(HEADSET, HDMI), default="alsa_output.usb-headset", hidden=False, text=""):
    def pactl(*args):
        if args[-1] == "sink-inputs":
            return streams
        if args[-1] == "sinks":
            return list(sinks)
        return default
    monkeypatch.setattr(probes, "_pactl", pactl)
    p = probes.Probe("no_sound", hidden=hidden)
    p.sample(GAME)
    return p.result(text)


def test_what_the_sound_check_tells_apart(monkeypatch):
    wine = "0130:err:xaudio2:XAudio2_Initialize failed to create the engine\n"
    r = sound(monkeypatch, [], text=wine)
    assert r["ok"] is False and r["cause"] == "no-stream" and "XAudio2_Initialize" in r["detail"]
    assert sound(monkeypatch, [stream(corked=True)])["cause"] == "silent"
    assert sound(monkeypatch, [stream(pid=4)])["cause"] == "no-stream"                 # another program's stream
    r = sound(monkeypatch, [stream()], sinks=({**HEADSET, "mute": True}, HDMI))
    assert r["cause"] == "muted-output" and "Headset, which is muted" in r["detail"]
    assert sound(monkeypatch, [stream(volume="0%")])["cause"] == "muted-stream"
    assert sound(monkeypatch, [stream(mute=True)], hidden=True)["ok"] is True          # (the hidden test muted it)
    r = sound(monkeypatch, [stream(sink=9)])
    assert r["cause"] == "other-output" and "TV (HDMI), not into your default output (Headset)" in r["detail"]
    r = sound(monkeypatch, [stream()])
    assert r["ok"] is True and r["detail"] == "the game plays sound into Headset"
    assert probes.from_text("x\n" + probes.marker(r))["cause"] == "ok"


def _input(sys_root: Path, n: int, name: str, phys: str, pad: bool, js: bool = False):
    """A fake /sys/class/input/eventN (and jsN) whose device sits under ``phys`` (a HID device path)."""
    dev = sys_root / "devices" / phys / "input" / f"input{n}"
    (dev / "capabilities").mkdir(parents=True)
    (dev / "name").write_text(name + "\n")
    # BTN_SOUTH (0x130) and ABS_X for a pad; a keyboard has keys but no axes
    (dev / "capabilities" / "key").write_text("1000000000000 0 0 0 0\n" if pad else "fffffffff\n")
    (dev / "capabilities" / "abs").write_text("3\n" if pad else "0\n")
    for kind in ["event"] + (["js"] if js else []):
        cls = sys_root / "class" / "input" / f"{kind}{n}"
        cls.mkdir(parents=True)
        (cls / "device").symlink_to(dev)
    return dev


def _hidraw(sys_root: Path, n: int, name: str, phys: str):
    hid = sys_root / "devices" / phys
    hid.mkdir(parents=True, exist_ok=True)
    (hid / "uevent").write_text(f"HID_NAME={name}\n")
    cls = sys_root / "class" / "hidraw" / f"hidraw{n}"
    cls.mkdir(parents=True)
    (cls / "device").symlink_to(hid)


def _opens(proc_root: Path, pid: int, *nodes: str):
    fd = proc_root / str(pid) / "fd"
    fd.mkdir(parents=True, exist_ok=True)
    for i, node in enumerate(nodes):
        (fd / str(i + 3)).symlink_to(node)


def test_controllers_seen_through_sys_and_proc(tmp_path):
    sys_root, proc = tmp_path / "sys", tmp_path / "proc"
    pad = "pci0000:00/usb1/1-2/1-2:1.3/0003:054C:0CE6.0003"
    _input(sys_root, 20, "Sony Interactive Entertainment DualSense Wireless Controller", pad, True, js=True)
    _input(sys_root, 21, "Keyboard", "pci0000:00/usb1/1-3/0003:1532:028E.0004", False)
    _input(sys_root, 30, "Microsoft X-Box 360 pad 0", "virtual", True)
    _hidraw(sys_root, 5, "DualSense Wireless Controller", pad)
    got = probes.connected_pads(sys_root)
    assert [p["name"] for p in got] == ["Sony Interactive Entertainment DualSense Wireless Controller",
                                        "Microsoft X-Box 360 pad 0"]
    assert got[0]["physical"] == "0003:054C:0CE6.0003" and got[1]["virtual"]
    # Wine opened the pad's hidraw and its event device: one controller, twice
    _opens(proc, 1001, "/dev/hidraw5", "/dev/input/event20", "/dev/input/event21")
    opened = probes.opened_inputs({1001}, proc, sys_root)
    by_node = {d["node"]: d for d in opened}
    assert by_node["/dev/hidraw5"]["physical"] == by_node["/dev/input/event20"]["physical"] == "0003:054C:0CE6.0003"
    assert not by_node["/dev/input/event21"]["pad"]


def pads(monkeypatch, connected, opened):
    monkeypatch.setattr(probes, "connected_pads", lambda sys_root=None: connected)
    monkeypatch.setattr(probes, "opened_inputs", lambda pids, proc_root=None, sys_root=None: opened)
    p = probes.Probe("controller")
    p.sample(GAME)
    return p.result()


def test_what_the_controller_check_tells_apart(monkeypatch):
    ds = {"name": "DualSense", "physical": "0003:054C:0CE6.0003", "virtual": False}
    xbox = {"name": "Microsoft X-Box 360 pad 0", "physical": "virtual:input30", "virtual": True}
    hid = {"node": "/dev/hidraw5", "kind": "hidraw", "name": "DualSense", "physical": ds["physical"], "pad": True}
    ev = {"node": "/dev/input/event20", "kind": "event", "name": "DualSense", "physical": ds["physical"], "pad": True}
    vev = {"node": "/dev/input/event30", "kind": "event", "name": xbox["name"], "physical": xbox["physical"], "pad": True}
    assert pads(monkeypatch, [], [])["ok"] is None                                     # nothing to check with
    r = pads(monkeypatch, [ds], [])
    assert r["cause"] == "none-seen" and "(DualSense)" in r["detail"]
    r = pads(monkeypatch, [ds], [hid, ev])
    assert r["cause"] == "doubled" and "DualSense twice (as event and hidraw)" in r["detail"]
    assert pads(monkeypatch, [ds, xbox], [ev, vev])["cause"] == "doubled-steam"
    r = pads(monkeypatch, [ds], [hid])
    assert r["ok"] is True and r["detail"] == "the game opened DualSense"


def _mangohud_log(path: Path, frametimes: list[float], gpu_load: float = 0.0, swap: tuple = (0.0, 0.0),
                  cpu: float = 30.0):
    rows = ["os,cpu,gpu,ram,kernel,driver,cpuscheduler", "Arch,Ryzen,Radeon,32000000,7.2,,performance",
            "fps,frametime,cpu_load,gpu_load,ram_used,swap_used,elapsed"]
    n = len(frametimes)
    for i, ft in enumerate(frametimes):
        sw = swap[0] + (swap[1] - swap[0]) * i / max(n - 1, 1)
        rows.append(f"{1000 / ft:.1f},{ft},{cpu},{gpu_load},12.5,{sw:.2f},{i}")
    path.write_text("\n".join(rows) + "\n")
    return path


def test_mangohuds_log_says_where_the_time_goes(tmp_path):
    # hitches only while it starts (every 40th frame of the first quarter): shaders being built
    p = probes.read_log(_mangohud_log(tmp_path / "a.csv", [80.0 if i % 40 == 0 and i < 400 else 16.6
                                                             for i in range(1600)]))
    assert p["spikes_early"] == 10 and p["spikes_late"] == 0
    assert probes.causes(p)[0][0] == "shaders"
    busy = probes.read_log(_mangohud_log(tmp_path / "b.csv", [25.0] * 600, gpu_load=99))
    assert [k for k, _w in probes.causes(busy)] == ["gpu"]
    waits = probes.read_log(_mangohud_log(tmp_path / "c.csv", [30.0] * 600, gpu_load=40))
    assert [k for k, _w in probes.causes(waits)] == ["cpu"]
    swapping = probes.read_log(_mangohud_log(tmp_path / "d.csv", [16.6] * 600, swap=(0.1, 1.4)))
    assert "memory" in [k for k, _w in probes.causes(swapping)]
    # all cores busy (a first start compiling shaders: SILENT HILL Townfall's real test run showed 86 %)
    hot = probes.read_log(_mangohud_log(tmp_path / "g.csv", [27.0] * 600, cpu=86))
    assert [k for k, _w in probes.causes(hot)] == ["cpu-busy"]
    smooth = probes.read_log(_mangohud_log(tmp_path / "e.csv", [8.3] * 600))
    assert probes.causes(smooth)[0][0] == "fine" and smooth["gpu_load"] is None    # (0: MangoHud could not read it)
    better, how = probes.smoother(smooth, waits)
    assert better and "1 % low 33 → 120 FPS" in how
    assert not probes.smoother(waits, smooth)[0]
    assert probes.read_log(_mangohud_log(tmp_path / "f.csv", [16.6] * 5)) is None    # too few frames
    assert probes.perf_from_text(probes.perf_marker(smooth))["fps"] == smooth["fps"]


def test_a_run_that_keeps_going_with_the_problem_is_no_success():
    judge = LibraryManager._judge_symptom
    me = SimpleNamespace(perf_baseline={})
    r = {"outcome": "ran", "detail": "kept running for 45s without crashing"}
    judge(me, 1, r, {"overrides": {}}, "no_sound", {"ok": False, "cause": "no-stream",
                                                    "detail": "the game never opened an audio stream"}, None)
    assert r["outcome"] == "symptom" and r["detail"] == "kept running, but the game never opened an audio stream"
    r = {"outcome": "ran", "detail": "kept running"}
    judge(me, 1, r, {"overrides": {}}, "controller", {"ok": None, "detail": "no controller is connected"}, None)
    assert r["outcome"] == "symptom" and "could not be checked: no controller" in r["detail"]
    r = {"outcome": "crashed", "detail": "stopped after 3s"}
    judge(me, 1, r, {"overrides": {}}, "no_sound", {"ok": False, "detail": "x"}, None)
    assert r["outcome"] == "crashed" and r["probe"]["detail"] == "x"                 # a crash stays a crash
    # runs badly: the current setup is the baseline, a fix must be measurably smoother
    slow = {"fps": 40.0, "low1": 18.0, "spikes": 30, "spikes_early": 10, "spikes_late": 20, "spikes_per_min": 40,
            "gpu_load": None, "swap_growth_gb": 0.0, "seconds": 30}
    r = {"outcome": "ran", "detail": "kept running"}
    judge(me, 7, r, {"overrides": {}}, "slow", None, slow)
    assert r["outcome"] == "symptom" and me.perf_baseline[7] is slow and "hitches" in r["detail"]
    same = {**slow, "low1": 19.0}
    r = {"outcome": "ran", "detail": "kept running"}
    judge(me, 7, r, {"overrides": {"options": ["no_fsync"]}}, "slow", None, same)
    assert r["outcome"] == "symptom" and "not smoother" in r["detail"]
    fast = {**slow, "fps": 60.0, "low1": 45.0, "spikes": 3}
    r = {"outcome": "ran", "detail": "kept running"}
    judge(me, 7, r, {"overrides": {"options": ["no_fsync"]}}, "slow", None, fast)
    assert r["outcome"] == "ran" and "smoother: 1 % low 18 → 45 FPS" in r["detail"]
    r = {"outcome": "ran", "detail": "kept running"}
    judge(me, 7, r, {"overrides": {}}, "slow", None, None)
    assert r["outcome"] == "symptom" and "could not be measured" in r["detail"]


def test_the_fixes_follow_what_the_run_saw():
    fixes = [fixgen.GeneratedFix(i, i, "") for i in ("sync:none", "input:sdl", "input:no-steaminput",
                                                       "input:no-hidraw", "audio:latency", "audio:native-xaudio")]

    def order(symptom, seen=None, perf=None):
        text = (probes.marker(seen) if seen else "") + (probes.perf_marker(perf) if perf else "")
        return [f.gen_id for f in fixgen._by_evidence(SimpleNamespace(everything=text), symptom, list(fixes))]
    assert order("controller", {"cause": "doubled-steam"})[0] == "input:no-steaminput"
    assert order("controller", {"cause": "none-seen"})[:2] == ["input:sdl", "input:no-hidraw"]
    assert not any(g.startswith("input:") for g in order("controller", {"cause": "none-connected"}))
    assert not any(g.startswith("audio:") for g in order("no_sound", {"cause": "other-output"}))
    assert order("no_sound", {"cause": "no-stream"})[:2] == ["audio:native-xaudio", "audio:latency"]
    stutter = {"fps": 50, "low1": 20, "spikes": 40, "spikes_early": 5, "spikes_late": 35, "spikes_per_min": 30,
               "gpu_load": None, "swap_growth_gb": 0.0}
    assert order("slow", perf=stutter)[0] == "sync:none"


def _diagnose(tmp_path: Path, body: str, symptom: str = "", measured=None):
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    exe = game / "Game.exe"
    exe.write_bytes(build_pe(["kernel32.dll"]))
    log = tmp_path / "game-1.log"
    log.write_text(f"\n=== 2026-10-23 10:00:00 GE-Proton · diagnostic run\n{body}")
    e = LibraryEntry(title="Game", status=LibraryStatus.INSTALLED, id=1, executable=str(exe), install_path=str(game))
    return {f["key"]: f for f in dg.diagnose(e, tmp_path / "pfx", log, protons=[], proton_name="", symptom=symptom,
                                             measured=measured).to_dict()["findings"]}


def test_diagnose_shows_what_the_probes_saw(tmp_path):
    keys = _diagnose(tmp_path, probes.marker({"symptom": "no_sound", "ok": False, "cause": "other-output",
                                              "detail": "the game plays into TV (HDMI), not into your default output "
                                                        "(Headset)", "facts": {}}))
    assert keys["sound-other-output"]["confidence"] >= 0.85 and not keys["sound-other-output"]["fixable"]
    keys = _diagnose(tmp_path, probes.marker({"symptom": "controller", "ok": False, "cause": "doubled",
                                              "detail": "the game opened DualSense twice", "facts": {}}))
    assert "pad-doubled" in keys
    # runs badly, nothing measured yet: measuring a real session is offered
    keys = _diagnose(tmp_path, "", symptom="slow")
    assert keys["perf-measure"]["fix"] == {"kind": "measure", "value": "next", "label": "Measure my next session"}
    played = {"fps": 48.0, "low1": 21.0, "spikes": 22, "spikes_early": 18, "spikes_late": 4, "spikes_per_min": 6.0,
              "gpu_load": None, "swap_growth_gb": 0.0, "seconds": 600}
    keys = _diagnose(tmp_path, "", symptom="slow", measured=played)
    assert "your last measured session" in keys["perf-shaders"]["explanation"] and "perf-measure" not in keys


async def test_a_measured_session_is_logged_and_read(core, tmp_path, monkeypatch):  # noqa: F811
    from ezgd.library import manager as mgr
    e, _root = install(core, tmp_path, {})
    monkeypatch.setattr(mgr.shutil, "which", lambda name: "/usr/bin/" + name if name == "mangohud" else None)
    lib = core.library
    assert lib.measure_next_session(e.id) and lib.measuring(e.id)
    env = lib._mangohud_env(lib.get(e.id))
    conf = Path(env["MANGOHUD_CONFIGFILE"]).read_text()
    assert "alpha=0" in conf and "no_display" not in conf          # (MangoHud logs nothing with no_display)
    assert f"output_folder={lib.measure_dir(e.id)}" in conf and "log_duration=900" in conf
    # the session's log arrives: it answers the request
    log = _mangohud_log(lib.measure_dir(e.id) / "Game_2026-10-23_10-00-00.csv", [16.6] * 600)
    os.utime(log, (log.stat().st_mtime + 5, log.stat().st_mtime + 5))
    p = lib.play_measurement(e.id)
    assert p["frames"] == 600 and not lib.measuring(e.id)
    env = lib._mangohud_env(lib.get(e.id))                         # back to the game's usual (live) config
    assert not env or "output_folder" not in Path(env["MANGOHUD_CONFIGFILE"]).read_text()
    assert await core._apply_fix(e.id, "measure", "next") and lib.measuring(e.id)


def test_a_watched_first_run_can_answer_without_testing_fixes():
    ask = lambda symptom, r, measure=True: asyncio.run(_answer(symptom, r, measure))   # noqa: E731

    async def _answer(symptom, r, measure):
        from ezgd.core.app_core import AppCore
        me = SimpleNamespace(library=SimpleNamespace(measure_next_session=lambda eid: measure))
        return await AppCore._symptom_answer(me, 1, symptom, r)
    ok = {"outcome": "ran", "detail": "kept running — the game plays sound into Headset",
          "probe": {"ok": True, "cause": "ok", "detail": "the game plays sound into Headset"}}
    v = ask("no_sound", ok)
    assert v["status"] == "seen" and v["reason"].startswith("In a test run the game plays sound into Headset.")
    routed = {"outcome": "symptom", "detail": "kept running, but …",
              "probe": {"ok": False, "cause": "other-output", "detail": "the game plays into TV (HDMI), not …"}}
    assert "that is your sound setup, not the game" in ask("no_sound", routed)["reason"]
    none = {"outcome": "symptom", "detail": "…", "probe": {"ok": None, "cause": "none-connected",
                                                           "detail": "no controller is connected — plug it in"}}
    assert ask("controller", none)["reason"] == "no controller is connected — plug it in"
    broken = {"outcome": "symptom", "detail": "…", "probe": {"ok": False, "cause": "no-stream", "detail": "…"}}
    assert ask("no_sound", broken) is None                                           # → fixes are tested
    assert ask("no_sound", {"outcome": "crashed", "detail": "…"}) is None
    smooth = {"outcome": "ran", "detail": "kept running — it ran smoothly here (90 FPS, 1 % low 70)"}
    assert "Your next session is measured" in ask("slow", smooth)["reason"]
