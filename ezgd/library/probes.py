"""Watching a test run for the problem the user reported, instead of only "did it keep running".

"No sound", "Controller doesn't work" and "Runs badly" are problems of a game that runs: judged by "it kept running",
the first test passed and the game was called fixed while nothing had changed. So a test run for one of them looks
at the problem itself while the game runs, and its verdict is what it saw:

* sound — the game's audio streams in PipeWire (found by process, as the hidden test mutes them): did it open one,
  did it play, into which output, muted or at 0 %? Wine's own audio errors say why when it opened none.
* controller — the input devices the run's processes opened (``/proc/<pid>/fd``): none of the connected
  controllers (Wine saw none), or the same controller twice (its hidraw and its event device, or the physical pad
  and Steam Input's virtual one — every button pressed twice).
* online — the run's network sockets, Wine's network / TLS errors, Steam running for an OnlineFix release, and the
  firewall's blocks of the game's ports (ezgd.library.netprobe).
* runs badly — MangoHud's log of the run (every game starts with MangoHud loaded, ezgd.library.manager
  ``_mangohud_env``): frame times, stutter, where it comes from; a fix is kept only when it is measurably smoother
  than the current setup.

Results become ``ezgd:probe {...}`` / ``ezgd:perf {...}`` lines in the run's log, which the fix generator and
Diagnose read like the system log's markers (ezgd.library.syslog).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

PROBED = {"no_sound", "controller", "slow", "online"}
MARK = "ezgd:probe "
PERF_MARK = "ezgd:perf "

_WINE_AUDIO = re.compile(r"^[0-9a-f]+:(?:err|warn):(?:mmdevapi|winepulse|winealsa|xaudio2|dsound|xact|faudio|"
                         r"dmusic|winmm|wave)[^\n]*", re.I | re.M)


# ------------------------------------------------------------------------------- sound
def _pactl(*args: str):
    if not shutil.which("pactl"):
        return None
    try:
        from ezgd.platform.hostenv import host_env
        out = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3, env=host_env()).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if args and args[0] == "-f":
        try:
            return json.loads(out or "[]")
        except ValueError:
            return None
    return out.strip()


def _volume(stream: dict) -> int:
    """The stream's loudest channel in percent."""
    best = 0
    for ch in (stream.get("volume") or {}).values():
        try:
            best = max(best, int(str(ch.get("value_percent", "0")).rstrip("%")))
        except (ValueError, AttributeError):
            continue
    return best


# ------------------------------------------------------------------------------- controllers
_HID_ID = re.compile(r"/([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4})(?:/|$)")
BTN_JOYSTICK, BTN_GAMEPAD, ABS_X, ABS_HAT0X = 0x120, 0x130, 0x00, 0x10


def _bits(text: str) -> int:
    """A /sys capabilities bitmap ("hex words, most significant first") as one integer (as ezgd.ui.gamepad reads
    it — the library does not import the interface)."""
    import struct
    n = 0
    for word in text.split():
        n = (n << (8 * struct.calcsize("l"))) | int(word, 16)
    return n


def _is_pad(sys_input: Path) -> bool:
    """An input device with gamepad/joystick buttons and a stick or d-pad (virtual keyboards claim every key, but
    have no axes)."""
    try:
        keys = _bits((sys_input / "capabilities" / "key").read_text())
        axes = _bits((sys_input / "capabilities" / "abs").read_text())
    except (OSError, ValueError):
        return False
    return bool(keys >> BTN_GAMEPAD & 1 or keys >> BTN_JOYSTICK & 1) and bool(axes >> ABS_X & 1 or axes >> ABS_HAT0X & 1)


def _physical(path: str) -> str:
    """The same controller seen through different device files has one key: its HID device (or, for a driver like
    xpad, the device above its input node; Steam Input's virtual pads are virtual)."""
    m = _HID_ID.search(path)
    if m:
        return m.group(1).upper()
    if "/devices/virtual/" in path:
        return "virtual:" + path.rsplit("/input/", 1)[-1].split("/")[0]
    return path.split("/input/")[0]


def connected_pads(sys_root: Path = Path("/sys")) -> list[dict]:
    """The controllers plugged in (or paired) now: {name, physical, virtual}."""
    out, seen = [], set()
    for ev in sorted((sys_root / "class" / "input").glob("event*")):
        dev = ev / "device"
        if not _is_pad(dev):
            continue
        real = os.path.realpath(dev)
        key = _physical(real)
        if key in seen:
            continue
        seen.add(key)
        try:
            name = (dev / "name").read_text().strip()
        except OSError:
            name = ev.name
        out.append({"name": name, "physical": key, "virtual": "/devices/virtual/" in real})
    return out


def opened_inputs(pids: set[int], proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")) -> list[dict]:
    """The input device files the run's processes hold open: {node, kind (event|js|hidraw), name, physical, pad}."""
    out, seen = [], set()
    for pid in pids:
        try:
            fds = list((proc_root / str(pid) / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            m = re.match(r"^/dev/(?:input/(event|js)(\d+)|(hidraw)(\d+))$", target)
            if not m or target in seen:
                continue
            seen.add(target)
            kind = m.group(1) or m.group(3)
            num = m.group(2) or m.group(4)
            if kind == "hidraw":
                sysdev = sys_root / "class" / "hidraw" / f"hidraw{num}" / "device"
                real = os.path.realpath(sysdev)
                pad = any(_is_pad(Path(i)) for i in Path(real).glob("input/input*"))
                try:
                    name = next((ln.split("=", 1)[1] for ln in (sysdev / "uevent").read_text().splitlines()
                                 if ln.startswith("HID_NAME=")), f"hidraw{num}")
                except OSError:
                    name = f"hidraw{num}"
            else:
                sysdev = sys_root / "class" / "input" / f"{kind}{num}" / "device"
                real = os.path.realpath(sysdev)
                pad = _is_pad(sysdev) if kind == "event" else True
                try:
                    name = (sysdev / "name").read_text().strip()
                except OSError:
                    name = f"{kind}{num}"
            out.append({"node": target, "kind": kind, "name": name, "physical": _physical(real), "pad": pad})
    return out


# ------------------------------------------------------------------------------- the probe
class Probe:
    """Samples one test run while it runs (``sample`` every second or two), then says what it saw (``result``)."""

    def __init__(self, symptom: str, hidden: bool = False):
        self.symptom = symptom
        self.hidden = hidden             # the hidden test mutes the game's streams itself: their mute is not the game's
        self.samples = 0
        self.streams: dict = {}          # index → {sink, playing, volume, muted}
        self.opened: dict = {}           # node → device
        self.pads: list[dict] = []
        self.started = time.time()
        self.ports: set = set()          # online: where the game listens
        self.connections = 0             # … and how many sockets talked to someone

    def sample(self, pids: set[int]) -> None:
        if not pids:
            return
        self.samples += 1
        try:
            if self.symptom == "no_sound":
                self._sample_audio(pids)
            elif self.symptom == "controller":
                if not self.pads:
                    self.pads = connected_pads()
                for dev in opened_inputs(pids):
                    self.opened.setdefault(dev["node"], dev)
            elif self.symptom == "online":
                from ezgd.library import netprobe
                socks = netprobe.sockets(pids)
                self.ports |= netprobe.listening(socks)
                self.connections = max(self.connections, sum(1 for s in socks if s["remote_port"]))
        except Exception:
            log.exception("sampling the test run failed")

    def _sample_audio(self, pids: set[int]) -> None:
        for st in _pactl("-f", "json", "list", "sink-inputs") or []:
            try:
                pid = int((st.get("properties") or {}).get("application.process.id") or 0)
            except ValueError:
                continue
            if pid not in pids:
                continue
            s = self.streams.setdefault(st.get("index"), {"sink": st.get("sink"), "playing": False, "volume": 0,
                                                          "muted": False})
            s["sink"] = st.get("sink")
            s["playing"] = s["playing"] or not st.get("corked", False)
            s["volume"] = max(s["volume"], _volume(st))
            s["muted"] = s["muted"] or bool(st.get("mute"))

    def result(self, text: str = "") -> dict:
        """{symptom, ok (True | False | None: could not be checked), cause, detail, facts}."""
        if self.symptom == "no_sound":
            return self._audio_result(text)
        if self.symptom == "controller":
            return self._controller_result()
        if self.symptom == "online":
            return self._online_result(text)
        return {"symptom": self.symptom, "ok": None, "cause": "", "detail": "", "facts": {}}

    def _online_result(self, text: str) -> dict:
        from ezgd.library import netprobe
        errors = netprobe.net_errors(text)
        fw = netprobe.firewall()
        found, _readable = netprobe.read_blocks(self.started, time.time(), self.ports)
        onlinefix = bool(re.search(r"onlinefix(64)?\.dll", text, re.I))
        out = {"symptom": "online", "facts": {"ports": sorted(f"{p}/{proto}" for proto, p in self.ports)[:20],
                                               "connections": self.connections, "firewall": fw, "blocks": found[:5],
                                               "net_errors": errors}}
        if onlinefix and not netprobe.steam_running():
            return {**out, "ok": False, "cause": "steam-off",
                    "detail": "OnlineFix connects through Steam, and Steam was not running"}
        if found:
            b = found[0]
            return {**out, "ok": False, "cause": "firewall",
                    "detail": f"{fw or 'the firewall'} blocked {b['packets']} incoming packet(s) to the game's "
                              f"{b['proto'].upper()} port {b['port']} — others cannot reach it",
                    "command": netprobe.allow_command(fw, b)}
        if errors and netprobe.is_tls(errors):
            return {**out, "ok": False, "cause": "tls",
                    "detail": f"its secure (TLS) connections failed under Wine (“{errors[0]}”)"}
        if not self.connections and not self.ports:
            return {**out, "ok": None, "cause": "no-network",
                    "detail": "it opened no network connection in the test run — it may connect only from its "
                              "online menu, which a test run does not reach"}
        return {**out, "ok": True, "cause": "ok",
                "detail": f"it connected ({self.connections} connection(s), listening on "
                          + (", ".join(out["facts"]["ports"][:4]) or "no port") + "); nothing was blocked"}

    def _audio_result(self, text: str) -> dict:
        wine = list(dict.fromkeys(m.group(0)[:200] for m in _WINE_AUDIO.finditer(text)))[:3]
        out = {"symptom": "no_sound", "facts": {"streams": len(self.streams), "wine_errors": wine}}
        if not self.samples:
            return {**out, "ok": None, "cause": "not-seen", "detail": "the run ended before its sound could be checked"}
        if not self.streams:
            return {**out, "ok": False, "cause": "no-stream",
                    "detail": "the game never opened an audio stream — its sound system did not start"
                    + (f" (Wine: “{wine[0]}”)" if wine else "")}
        sinks = {s.get("index"): s for s in _pactl("-f", "json", "list", "sinks") or []}
        default = _pactl("get-default-sink") or ""
        playing = [s for s in self.streams.values() if s["playing"]]
        if not playing:
            return {**out, "ok": False, "cause": "silent",
                    "detail": "the game opened an audio stream but never played anything into it"}
        s = playing[0]
        sink = sinks.get(s["sink"]) or {}
        where = sink.get("description") or sink.get("name") or "an output"
        out["facts"].update(output=where, output_name=sink.get("name", ""), default=default)
        if sink.get("mute"):
            return {**out, "ok": False, "cause": "muted-output", "detail": f"the game plays into {where}, which is muted"}
        if not self.hidden and (s["muted"] or s["volume"] == 0):
            return {**out, "ok": False, "cause": "muted-stream",
                    "detail": "the game's own sound stream is muted or at 0 % in the volume mixer"}
        if default and sink.get("name") and sink["name"] != default:
            dflt = next((x.get("description") for x in sinks.values() if x.get("name") == default), default)
            return {**out, "ok": False, "cause": "other-output",
                    "detail": f"the game plays into {where}, not into your default output ({dflt})"}
        return {**out, "ok": True, "cause": "ok", "detail": f"the game plays sound into {where}"}

    def _controller_result(self) -> dict:
        pads = self.pads or connected_pads()
        opened = [d for d in self.opened.values() if d["pad"]]
        out = {"symptom": "controller", "facts": {"connected": [p["name"] for p in pads],
                                                   "opened": [f"{d['name']} ({d['kind']})" for d in opened]}}
        if not pads:
            return {**out, "ok": None, "cause": "none-connected",
                    "detail": "no controller is connected — plug it in (or switch it on) and test again"}
        if not opened:
            return {**out, "ok": False, "cause": "none-seen",
                    "detail": "the game did not open any of the connected controllers ("
                    + ", ".join(p["name"] for p in pads) + ") — it does not see them"}
        by_pad: dict[str, list[dict]] = {}
        for d in opened:
            by_pad.setdefault(d["physical"], []).append(d)
        doubled = [ds for ds in by_pad.values() if len({d["kind"] for d in ds if d["kind"] != "js"}) > 1]
        physical = [p for p in pads if not p["virtual"]]
        virtual_opened = [d for d in opened if d["physical"].startswith("virtual:")]
        if doubled:
            ds = doubled[0]
            return {**out, "ok": False, "cause": "doubled",
                    "detail": f"the game opened {ds[0]['name']} twice (as " + " and ".join(
                        sorted({d['kind'] for d in ds})) + ") — it sees two controllers, every button pressed twice"}
        if virtual_opened and any(not d["physical"].startswith("virtual:") for d in opened) and physical:
            return {**out, "ok": False, "cause": "doubled-steam",
                    "detail": f"the game opened both {physical[0]['name']} and Steam Input's virtual pad "
                              f"({virtual_opened[0]['name']}) — two controllers for one"}
        return {**out, "ok": True, "cause": "ok",
                "detail": "the game opened " + ", ".join(sorted({d["name"] for d in opened}))}


def marker(res: dict) -> str:
    return MARK + json.dumps(res, sort_keys=True) + "\n"


def from_text(text: str) -> dict | None:
    """The probe's result in a run's log (the last one), or None."""
    found = None
    for m in re.finditer(r"^" + re.escape(MARK) + r"(\{.*\})\s*$", text, re.M):
        try:
            found = json.loads(m.group(1))
        except ValueError:
            continue
    return found


# ------------------------------------------------------------------------------- runs badly: MangoHud's log
MEASURE_OPTS = ("alpha=0", "background_alpha=0", "font_size=1")    # (MangoHud 0.8 logs nothing with no_display)


def logging_conf(conf: Path, folder: Path, start: int, duration: int, base: str = "", visible: bool = False) -> Path:
    """A MangoHud config that logs a run into ``folder``: the user's own config when the overlay is shown,
    otherwise an invisible one — MangoHud does not log at all while hidden with ``no_display``."""
    lines = [ln for ln in base.splitlines()
             if ln.strip().split("=", 1)[0].strip() not in ("no_display", "autostart_log", "log_duration",
                                                             "output_folder", "log_interval")] if visible else []
    if not visible:
        lines += list(MEASURE_OPTS)
    lines += [f"autostart_log={start}", f"log_duration={duration}", f"output_folder={folder}"]
    folder.mkdir(parents=True, exist_ok=True)
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("\n".join(lines).strip() + "\n", "utf-8")
    return conf


def newest_log(folder: Path, since: float = 0.0) -> Path | None:
    try:
        logs = [p for p in folder.glob("*.csv") if not p.name.endswith("_summary.csv") and p.stat().st_mtime >= since]
    except OSError:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime, default=None)


def read_log(path: Path) -> dict | None:
    """MangoHud's log → the numbers that say how it ran (None when it holds too few frames).

    Columns differ between MangoHud versions, so they are read by name; GPU load reads 0 where MangoHud cannot
    measure it (it says nothing then)."""
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return None
    head = next((i for i, r in enumerate(rows) if "fps" in r and "frametime" in r), None)
    if head is None:
        return None
    cols = {name: i for i, name in enumerate(rows[head])}
    info = dict(zip(rows[0], rows[1])) if head >= 2 else {}

    def col(name: str) -> list[float]:
        i = cols.get(name)
        out = []
        if i is None:
            return out
        for r in rows[head + 1:]:
            try:
                out.append(float(r[i]))
            except (ValueError, IndexError):
                continue
        return out
    ft = [x for x in col("frametime") if x > 0]
    if len(ft) < 30:
        return None
    srt = sorted(ft)
    med = statistics.median(ft)
    p99 = srt[min(len(srt) - 1, int(len(srt) * 0.99))]
    spike = max(med * 2.5, med + 8.0)
    half = len(ft) // 2
    early = sum(1 for x in ft[:half] if x > spike)
    late = sum(1 for x in ft[half:] if x > spike)
    total_ms = sum(ft)
    gpu = [x for x in col("gpu_load")]
    cpu = col("cpu_load")
    swap = col("swap_used")
    ram = col("ram_used")
    vram = col("gpu_vram_used")
    return {
        "frames": len(ft), "seconds": round(total_ms / 1000, 1),
        "fps": round(len(ft) / (total_ms / 1000), 1) if total_ms else 0.0,
        "low1": round(1000 / p99, 1) if p99 else 0.0,                     # "1 % low": the slowest 1 % of frames
        "median_ms": round(med, 2), "p99_ms": round(p99, 2),
        "spikes": early + late, "spikes_early": early, "spikes_late": late,
        "spikes_per_min": round((early + late) / max(total_ms / 60000, 1 / 60), 1),
        "gpu_load": round(statistics.mean(gpu), 1) if gpu and max(gpu) > 0 else None,
        "cpu_load": round(statistics.mean(cpu), 1) if cpu else None,
        "swap_growth_gb": round(max(swap) - min(swap), 2) if swap else 0.0,
        "ram_gb": round(max(ram), 1) if ram else None,
        "vram_gb": round(max(vram), 1) if vram and max(vram) > 0 else None,
        "gpu": info.get("gpu", ""), "cpu": info.get("cpu", ""),
    }


def causes(p: dict) -> list[tuple[str, str]]:
    """What the numbers say slows it down: [(key, in words)], the clearest first."""
    out = []
    if p["spikes"] >= 5 and p["spikes_early"] >= 2 * max(p["spikes_late"], 1):
        out.append(("shaders", f"stutter that fades as it runs ({p['spikes_early']} hitches early, "
                               f"{p['spikes_late']} later) — the pattern of shaders being built the first time"))
    elif p["spikes_per_min"] >= 6:
        out.append(("stutter", f"{p['spikes']} hitches ({p['spikes_per_min']} a minute) over the whole run"))
    if p.get("swap_growth_gb", 0) >= 0.5:
        out.append(("memory", f"the computer moved {p['swap_growth_gb']} GB into swap while it ran — memory is short"))
    cl = p.get("cpu_load")
    if cl is not None and cl >= 85:            # (all cores together: on a first start, shaders are compiled on all)
        out.append(("cpu-busy", f"the processor is {cl:.0f} % busy across all its cores — on a first start that is "
                                "shaders being compiled; if it stays that way, the processor is the limit"))
    gl = p.get("gpu_load")
    if gl is not None and gl >= 92:
        out.append(("gpu", f"the graphics card is fully busy ({gl:.0f} %) — it is the limit"))
    elif gl is not None and gl < 60 and p["fps"] < 55:
        out.append(("cpu", f"the graphics card waits ({gl:.0f} % busy) at {p['fps']:.0f} FPS — the processor (or the "
                           "game's own engine) is the limit"))
    if not out and p["fps"] >= 55 and p["low1"] >= 40:
        out.append(("fine", f"it ran smoothly here ({p['fps']:.0f} FPS, 1 % low {p['low1']:.0f})"))
    return out


def smoother(after: dict, before: dict) -> tuple[bool, str]:
    """Is ``after`` measurably smoother than ``before``? (≥ 15 % better 1 % low or average, or a third fewer hitches,
    without being worse otherwise.)"""
    lo = after["low1"] / before["low1"] if before.get("low1") else 1.0
    fps = after["fps"] / before["fps"] if before.get("fps") else 1.0
    hitch = (after["spikes"] + 1) / (before["spikes"] + 1)
    better = (lo >= 1.15 or fps >= 1.15 or hitch <= 0.67) and lo >= 0.9 and fps >= 0.9
    how = f"1 % low {before['low1']:.0f} → {after['low1']:.0f} FPS, average {before['fps']:.0f} → {after['fps']:.0f}, " \
          f"hitches {before['spikes']} → {after['spikes']}"
    return better, how


def perf_marker(p: dict) -> str:
    return PERF_MARK + json.dumps(p, sort_keys=True) + "\n"


def perf_from_text(text: str) -> dict | None:
    found = None
    for m in re.finditer(r"^" + re.escape(PERF_MARK) + r"(\{.*\})\s*$", text, re.M):
        try:
            found = json.loads(m.group(1))
        except ValueError:
            continue
    return found


# ------------------------------------------------------------- the frame meter (every test run, ezgd.library.manager)
# A test's screenshots can be black while the game draws (Ghost Recon Wildlands' hidden window always captured black)
# — and that once decided Fix my game for the wrong Proton. MangoHud's log of the run says it directly: one row per
# frame presented, written as it goes (a crash or a kill keeps the frames until then — checked with vkcube on
# 2026-10-27: a SIGKILL at 4 s left 329 rows; ``elapsed`` is nanoseconds since the log started). It only sees Vulkan
# presents — DXVK and vkd3d-proton count, OpenGL and WineD3D do not — so a missing log says nothing; a log with
# (almost) no frames does.
FRAME_START = 1                 # seconds after MangoHud loads that its log starts
FRAMES_MARK = "ezgd:frames "
STALL_SECONDS = 15              # no frame for this long while it still runs: it froze (or sits in a long load)
_LOG_TIME = re.compile(r"_(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.csv$")


def frames_conf(conf: Path, folder: Path, duration: int) -> Path:
    """An invisible MangoHud that logs every frame of a test run from its start."""
    return logging_conf(conf, folder, FRAME_START, duration)


def _frame_times(path: Path) -> list[float] | None:
    """When each frame of a MangoHud log was presented (seconds since the log started), or None (not such a log)."""
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return None
    head = next((i for i, r in enumerate(rows) if "frametime" in r), None)
    if head is None:
        return None
    ft_i = rows[head].index("frametime")
    el_i = rows[head].index("elapsed") if "elapsed" in rows[head] else None
    out: list[float] = []
    total = 0.0
    for r in rows[head + 1:]:
        try:
            ft = float(r[ft_i])
            total += ft / 1000
            at = float(r[el_i]) / 1e9 if el_i is not None else total
        except (ValueError, IndexError):
            continue
        if ft > 0:
            out.append(at)
    return out


def read_frames(folder: Path, run_start: float) -> dict | None:
    """The run's frames, from MangoHud's logs in ``folder`` (one per process that presented with Vulkan — the one with
    the most frames is the game): {"frames", "first", "last", "gap", "process"}, the times in seconds since
    ``run_start`` (wall clock). None: no log at all (MangoHud not loaded, or a game it cannot see)."""
    try:
        paths = [p for p in folder.glob("*.csv")
                 if not p.name.endswith("_summary.csv") and p.stat().st_mtime >= run_start - 1]
    except OSError:
        return None
    best = None
    for p in paths:
        times = _frame_times(p)
        if times is None:
            continue
        m = _LOG_TIME.search(p.name)
        try:
            began = time.mktime(time.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")) if m else None
        except ValueError:
            began = None
        offset = began - run_start if began is not None else float(FRAME_START)
        gap = max((b - a for a, b in zip(times, times[1:])), default=0.0)
        found = {"frames": len(times), "first": round(offset + times[0], 1) if times else None,
                 "last": round(offset + times[-1], 1) if times else None, "gap": round(gap, 1),
                 "process": m.string[:m.start()] if m else p.stem}
        if best is None or found["frames"] > best["frames"]:
            best = found
    return best


def frame_verdict(meter: dict | None, run_seconds: int, still: bool, vulkan: bool) -> dict:
    """What the frame meter says about a run: {"drawing": True/False/None, "stalled": seconds without a frame at the
    end while it still ran (0: none), "text"}. ``vulkan``: the game presents through Vulkan (DXVK, vkd3d-proton,
    Vulkan itself) — only then do no frames mean it drew nothing."""
    if meter is None:
        return {"drawing": None, "stalled": 0, "text": ""}
    if meter["frames"] < 10:
        if not vulkan:
            return {"drawing": None, "stalled": 0, "text": ""}
        return {"drawing": False, "stalled": 0, "text": "it presented no frames (MangoHud counted "
                                                         f"{meter['frames']})"}
    stalled = max(0, int(run_seconds - meter["last"])) if still else 0
    if stalled >= STALL_SECONDS:
        return {"drawing": True, "stalled": stalled,
                "text": f"it drew {meter['frames']} frames from {meter['first']:.0f} s, then none for the last "
                        f"{stalled} s (frozen, or a long load)"}
    if still:
        return {"drawing": True, "stalled": 0,
                "text": f"it drew {meter['frames']} frames (from {meter['first']:.0f} s on, MangoHud)"}
    return {"drawing": True, "stalled": 0, "text": f"it drew frames from {meter['first']:.0f} s until "
                                                   f"{meter['last']:.0f} s"}


def frames_marker(meter: dict) -> str:
    return FRAMES_MARK + json.dumps(meter, sort_keys=True) + "\n"


def frames_from_text(text: str) -> dict | None:
    found = None
    for m in re.finditer(r"^" + re.escape(FRAMES_MARK) + r"(\{.*\})\s*$", text, re.M):
        try:
            found = json.loads(m.group(1))
        except ValueError:
            continue
    return found
