"""Following the error: read one detailed run the way a person debugging it would, instead of checking a list.

A diagnostic run's Wine log (``+loaddll,+seh,+module,+msgbox``) records which files the game loaded and from where,
every exception and where it happened, and every message box. ``trace`` turns it into the facts that matter — the
game's own process, how it ended, what happened just before, which of the game's own DLLs Wine swapped for its
built-in ones, which add-on files never loaded — and ``follow`` asks "why?" from the end backwards, producing a chain
of reasons and the suspects it points at. Each suspect carries a test (a change tried in a throw-away copy) and a
prediction of what that test must show if the suspect is right ("winmm.dll loads from the game's folder and
OnlineFix64.dll is loaded"). ``verdict`` compares the run before and after: a change whose prediction came true, or
that took the game further, is kept and the next run's first error is followed in turn — Expedition 33 needed
exactly that chain (OnlineFix's loader replaced → crash before the game's log; loader kept → OnlineFix's one-time
notice; notice passed → main menu).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ezgd.library import errorbox

_LOAD = re.compile(r'^([0-9a-f]+):trace:loaddll:\w+ Loaded L"([^"]+)" at ([0-9A-Fa-f]{8,16}): (native|builtin)', re.M)
_EXC = re.compile(r"^([0-9a-f]+):trace:seh:dispatch_exception code=([0-9a-f]{8})\b[^\n]*addr=([0-9A-Fa-f]+)", re.M)
_UNHANDLED = re.compile(r"Unhandled exception code ([0-9a-f]{8}) flags \d+ addr 0x([0-9a-f]+)", re.I)
_BACKTRACE = re.compile(r'^([0-9a-f]+):warn:seh:virtual_unwind backtrace: [0-9A-Fa-f]+: L"([^"]+)" \+ ([0-9A-Fa-f]+)',
                        re.M)
_MISSING = re.compile(r'Library ([\w.\-]+\.dll) \(which is needed by L?"?([^")\n]+)', re.I)
_UNIMPL = re.compile(r"unimplemented function ([\w.\-]+\.dll\.[\w@?$]+)", re.I)
# warn+file: a file the program looked for and did not find (Wine logs it twice: lookup_unix_name and NtCreateFile)
_FILE_MISS = re.compile(r'^([0-9a-f]+):(?:warn:file:[^\n]*?L"\\\\\?\?\\\\([^"]+)"[^\n]*?not found \((?:c0000034|c000003a)\)|'
                        r'ezgd:missing-file L"([^"]+)")', re.M)
# +reg (a "look closer" run): a key lookup, and its answer on the next line of the same thread
# a closer look traces who ended the game or started another program (manager.RELAY_CALLS)
_RELAY_CALL = re.compile(r"^([0-9a-f]+):Call ([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\((.*)\) ret=([0-9a-f]+)\s*$", re.M)
_RELAY_RET = re.compile(r"^([0-9a-f]+):Ret  *([A-Za-z0-9_]+)\.(CreateProcess[AW]|ShellExecute[A-Za-z]*)\(\) "
                        r"retval=([0-9a-f]+)", re.M)
_EXIT_FUNCS = {"exitprocess", "rtlexituserprocess", "exit", "_exit", "abort", "terminateprocess",
               "ntterminateprocess"}
_LAUNCH_FUNCS = {"createprocessw", "createprocessa", "shellexecutew", "shellexecutea", "shellexecuteexw"}
_WSTR = re.compile(r'L?"((?:[^"\\]|\\.)*)"')
_STEAM_FAIL = re.compile(r"(\[S_API FAIL\][^\n]*|SteamAPI_Init\(\) failed[^\n]*|No appID found[^\n]*|"
                         r"Steam must be running[^\n]*|SteamAPI_RestartAppIfNecessary[^\n]*|"
                         r"Failed to load original steamclient[^\n]*)", re.I)
_GAMELOG = "ezgd-gamelog: "
_KEY_MARK = re.compile(r'^([0-9a-f]+):ezgd:missing-key L"([^"]*)"', re.M)
_REG = re.compile(r'^([0-9a-f]+):trace:reg:NtOpenKeyEx (?:\(([^,]+),L"([^"]*)"|<- (\(nil\)|0x[0-9a-f]+))', re.M)
_FILE_NOISE = re.compile(r"(\.(dll|exe|drv|mui|manifest|local|config|lnk|tmp|sys|ocx|ax|pdb|ttf|ttc|otf|fon|nls|cat|inf|"
                         r"pnf|icm|cpl|tlb|olb|sdb|etl|pf)$|^c:\\windows\\|^c:\\program files( \(x86\))?\\common files\\|"
                         r"\\desktop\.ini$|^unix\\|^pipe\\|\\appdata\\(local|roaming)\\microsoft\\|"
                         r"^[a-z]:\\(autorun\.inf|\$recycle\.bin)|\\nvidia corporation\\|\\steam\\(logs|config)\\)", re.I)
_KEY_NOISE = re.compile(r"^(software\\(wow6432node\\)?(wine|microsoft|classes|policies|khronos|nvidia corporation|"
                        r"nvidia|intel|amd|ati technologies|clients|registeredapplications|odbc|mozilla|google)\b|"
                        r"system\\|control panel|\\registry\\|clsid|interface|appid|typelib|keyboard layout|"
                        r"environment|hardware\\|sam\\|security\\|mime\\|\.|s-1-5-|drivers|"
                        r"software\\wow6432node\\classes|\*|directshow|media type|filter|wow6432node\\clsid)",
                        re.I)
# exceptions games raise and handle all the time (thread names, C++ throws they catch, debugger probes)
_BENIGN = {"406d1388", "40010006", "4001000a", "80000003", "e06d7363", "000006ba", "000006a6", "c0000008",
           "e0434352", "e0434f4d"}
_FATAL = {"c0000005": "an access violation", "c0000409": "a security check failure (a crash the game detected)",
          "c00000fd": "a stack overflow", "c000001d": "an illegal CPU instruction",
          "c0000094": "a division by zero", "c0000096": "a privileged instruction", "c0000374": "heap corruption",
          "c0000420": "an assertion failure"}
# after an exception, these mean nothing caught it: the crash handler / the process's last-chance filter ran
_LAST_CHANCE = re.compile(r"(_XcptFilter|UnhandledExceptionFilter|RtlCaptureStackBackTrace|MiniDumpWriteDump|"
                          r"EnumProcessModules|Unhandled exception)")

# add-on loaders a release puts next to the game: the proxy DLLs they use, the files that mark them, what they load
LOADERS = [
    {"name": "OnlineFix", "proxies": ("winmm", "version", "winhttp", "dinput8", "dsound", "iphlpapi", "wininet"),
     "markers": ("OnlineFix64.dll", "OnlineFix.dll", "OnlineFix.ini"), "payload": ("onlinefix64.dll", "onlinefix.dll")},
    {"name": "UE4SS (Unreal mod loader)", "proxies": ("dwmapi", "xinput1_3"),
     "markers": ("ue4ss/UE4SS.dll", "UE4SS.dll", "UE4SS-settings.ini"), "payload": ("ue4ss.dll",)},
    {"name": "BepInEx (Unity mod loader)", "proxies": ("winhttp", "version", "winmm"),
     "markers": ("BepInEx", "doorstop_config.ini", ".doorstop_version"), "payload": ()},
    {"name": "MelonLoader (Unity mod loader)", "proxies": ("version", "winmm", "winhttp"),
     "markers": ("MelonLoader",), "payload": ("melonloader.bootstrap.dll", "bootstrap.dll")},
    {"name": "an ASI loader (script mods)", "proxies": ("dinput8", "winmm", "version", "d3d9", "dsound", "xinput1_3",
                                                         "winhttp", "vorbisfile"),
     "markers": ("*.asi", "scripts/*.asi", "plugins/*.asi"), "payload": (".asi",)},
    {"name": "ReShade", "proxies": ("dxgi", "d3d9", "d3d11", "d3d12", "opengl32"),
     "markers": ("ReShade.ini", "reshade-shaders", "ReShade.log"), "payload": ()},
]


@dataclass
class Load:
    tid: str
    path: str                  # as Wine logged it (C:\..., X:\...)
    name: str                  # lower-case file name
    base: int
    kind: str                  # native | builtin
    at: int                    # position in the log


@dataclass
class Crash:
    tid: str
    code: str
    what: str
    module: str = ""
    offset: int = 0
    kind: str = ""
    access: str = ""           # read | write | execute
    address: int | None = None
    at: int = 0
    certain: bool = False      # Wine / the game's crash handler confirms nothing caught it

    @property
    def null(self) -> bool:
        return self.address is not None and self.address < 0x10000

    def describe(self) -> str:
        where = f" in {self.module}" + (f" (+0x{self.offset:x})" if self.offset else "") if self.module else ""
        how = ""
        if self.code == "c0000005" and self.access:
            how = f" {self.access}ing " + ("address 0 (a missing object: something it expected was not set up)"
                                           if self.null else f"0x{self.address:x}")
        return f"{self.what}{how}{where}"


@dataclass
class Replaced:
    name: str                  # winmm.dll
    folder: str                # the game folder it is in (Windows path as logged)
    loader: str = ""           # whose loader it is (OnlineFix, UE4SS …), when known
    payload_missing: list[str] = field(default_factory=list)   # what that loader should have loaded and did not


@dataclass
class Trace:
    exe: str = ""
    game_tids: set[str] = field(default_factory=set)
    loads: list[Load] = field(default_factory=list)
    replaced: list[Replaced] = field(default_factory=list)
    crash: Crash | None = None
    missing: list[tuple[str, str]] = field(default_factory=list)      # (dll, needed by)
    unimplemented: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)                   # error boxes (not notices)
    notices: list[str] = field(default_factory=list)
    phase: list[str] = field(default_factory=list)
    last_words: list[str] = field(default_factory=list)
    exit_code: int | None = None
    seconds: int | None = None
    ran: bool = False                                                  # still running when the test ended
    files_missing: list[tuple[str, str, int]] = field(default_factory=list)   # (thread, path, position) the game
    keys_missing: list[tuple[str, str, int]] = field(default_factory=list)    # looked for and did not find
    keys_traced: bool = False                                          # a "look closer" run (registry recorded)
    exits: list[dict] = field(default_factory=list)       # who ended a process: {module, offset, func, code, self}
    launches: list[dict] = field(default_factory=list)    # who started what: {module, func, target}
    game_errors: list[tuple[str, str]] = field(default_factory=list)   # (log file, line) from the game's own logs
    steam_fail: list[str] = field(default_factory=list)   # what Steam's API said when it refused
    setup: dict = field(default_factory=dict)                          # the app's snapshot of how it was started
    system: list = field(default_factory=list)            # what the kernel / graphics driver logged (syslog.Event)
    system_readable: bool = True                          # False: the system log could not be read for this run

    def loaded(self, name: str) -> bool:
        name = name.lower()
        return any(ld.name == name or (name.startswith(".") and ld.name.endswith(name)) for ld in self.loads)

    def loaded_native(self, name: str) -> bool:
        name = name.lower()
        return any(ld.name == name and ld.kind == "native" for ld in self.loads)

    def serious(self) -> list:
        """What the system logged that ended or broke this run, the deepest cause first: killed for memory, then a
        GPU fault (a game crash on a "device lost" usually follows one), then a crash Wine could not catch. A GPU
        fault no process was named for counts only when the run failed; NVIDIA's Xid 45 is the clean-up after
        another fault."""
        failed = not self.ran or bool(self.errors) or self.crash is not None
        out = [e for e in self.system if e.kind in ("oom", "oomd", "gpu", "segfault", "core")
               and (e.ours or failed) and e.code != "nvidia:45"]
        order = {"oom": 0, "oomd": 1, "gpu": 2, "segfault": 3, "core": 4}
        return sorted(out, key=lambda e: order[e.kind])

    def first_problem(self) -> str:
        """The first thing that went wrong, in a few words (what the next step follows)."""
        if self.errors:
            return f"error message “{self.errors[0][:120]}”"
        if self.missing:
            return f"{self.missing[0][0]} not found"
        if self.serious():
            return self.serious()[0].describe()
        if self.crash is not None:
            return self.crash.describe()
        if self.replaced:
            return "Wine replaced " + ", ".join(r.name for r in self.replaced)
        if self.notices:
            return f"a notice waiting for OK (“{self.notices[0][:80]}”)"
        if self.ran:
            return "none — it kept running"
        quit_ = next((x for x in self.exits if x["self"] and x["module"]), None)
        if quit_ is not None:
            return f"{quit_['module']} ended it ({quit_['func']}({quit_['code']}))"
        if self.exit_code is not None:
            return f"it quit with code {self.exit_code} after {self.seconds}s"
        return "nothing recorded"

    def score(self) -> tuple:
        """How far the run got, comparable between runs: milestones, then not crashing, then time alive."""
        return (len(self.phase), self.ran, self.crash is None and not self.errors and not self.serious(),
                min(self.seconds or 0, 45) // 10)


def _win_dir(path: str) -> str:
    """'X:\\Games\\A\\b.dll' → '/games/a' (the drive letter dropped: Wine maps drives to different folders)."""
    p = path.replace("\\\\", "\\").replace("\\", "/")
    p = re.sub(r"^[A-Za-z]:", "", p)
    return p.rsplit("/", 1)[0].lower()


def _in(folder_win: str, dirs: list[Path]) -> Path | None:
    for d in dirs:
        if str(d).lower().rstrip("/").endswith(folder_win) and folder_win:
            return d
    return None


def _under(folder_win: str, root: Path) -> bool:
    """Is a logged Windows folder inside ``root`` (any depth)? The drive letter is unknown, so the folder's
    leading parts are matched against the end of ``root``."""
    parts = [x for x in folder_win.split("/") if x]
    r = str(root).lower().rstrip("/")
    return any(r.endswith("/" + "/".join(parts[:i])) for i in range(len(parts), 0, -1))


def _marker_present(folder: Path, marker: str) -> bool:
    if "*" in marker:
        return any(True for _ in folder.glob(marker))
    return (folder / marker).exists()


def trace(text: str, exe: Path, *, exit_code: int | None = None, seconds: int | None = None, ran: bool = False,
          extra: str = "") -> Trace:
    """The facts of one run. ``extra``: other logs of the same run (the engine's, a mod loader's) for how far it
    got."""
    from ezgd.library import history
    from ezgd.platform.proton import game_binary_dirs
    exe = Path(exe)
    t = Trace(exe=str(exe), exit_code=exit_code, seconds=seconds, ran=ran)
    dirs = game_binary_dirs(exe)
    exe_names = {exe.name.lower()} | {p.name.lower() for d in dirs[1:] for p in d.glob("*-Shipping.exe")}
    for m in _LOAD.finditer(text):
        path = m.group(2).replace("\\\\", "\\")
        ld = Load(m.group(1), path, path.split("\\")[-1].lower(), int(m.group(3), 16), m.group(4), m.start())
        t.loads.append(ld)
        if ld.kind == "native" and (ld.name in exe_names or (ld.name.endswith(".exe")
                                                            and _under(_win_dir(ld.path), exe.parent))):
            t.game_tids.add(ld.tid)           # (the game's exe — and any program it starts from its own folder)
    game = [ld for ld in t.loads if ld.tid in t.game_tids] or t.loads
    # the game's own DLLs Wine swapped for its built-in ones (a mod loader, OnlineFix's loader …)
    seen: set[str] = set()
    for ld in game:
        if ld.kind != "builtin" or not ld.name.endswith(".dll") or ld.name in seen:
            continue
        folder = _in(_win_dir(ld.path), dirs)
        if folder is None:
            continue
        seen.add(ld.name)
        r = Replaced(ld.name, ld.path.rsplit("\\", 1)[0])
        stem = ld.name[:-4]
        for lo in LOADERS:
            if stem in lo["proxies"] and any(_marker_present(folder, mk) for mk in lo["markers"]):
                r.loader = lo["name"]
                r.payload_missing = [p for p in lo["payload"] if not t.loaded(p)]
                if lo["name"] == "OnlineFix":
                    listed = folder / "dlllist.txt"        # OnlineFix names what its loader loads
                    try:
                        r.payload_missing = [n.strip().lower() for n in listed.read_text("utf-8", "replace").split()
                                             if n.strip().lower().endswith(".dll") and not t.loaded(n.strip())] \
                            or r.payload_missing
                    except OSError:
                        pass
                break
        t.replaced.append(r)
    t.crash = _crash(text, t)
    t.missing = [(m.group(1).lower(), m.group(2).replace("\\\\", "\\").split("\\")[-1])
                 for m in _MISSING.finditer(text)][:5]
    t.unimplemented = list(dict.fromkeys(m.group(1) for m in _UNIMPL.finditer(text)))[:5]
    _lookups(text, t, dirs)
    _calls(text, t, dirs)
    _game_logs(extra, t)
    t.steam_fail = list(dict.fromkeys(m.group(1).strip()[:200] for m in _STEAM_FAIL.finditer(text + "\n" + extra)))[:3]
    t.setup = _setup(text)
    from ezgd.library import syslog
    t.system, t.system_readable = syslog.from_text(text + "\n" + extra)
    t.errors = errorbox.messages(text)
    t.notices = [m for m in errorbox.messages(text, 10, notices=True) if errorbox.is_notice(m)]
    t.phase = history.phase(text + "\n" + extra).reached
    try:
        from ezgd.library.diagnose import _last_words
        t.last_words = _last_words(text)
    except Exception:
        t.last_words = []
    return t


_DUMP_MARK = re.compile(r"^ezgd:dump code=([0-9a-f]{8}) module=(\S*) offset=([0-9a-f]+) access=(\w*) "
                        r"address=([0-9a-f]*)", re.M)


def dump_line(d) -> str:
    """A crash dump (ezgd.library.minidump) as a line the trace reads like Wine's own crash report."""
    mod = d.module.name if d.module is not None else ""
    access = {0: "read", 1: "write", 8: "execute"}.get(d.access, "")
    return (f"ezgd:dump code={(d.code or 0):08x} module={mod} offset={(d.offset or 0):x} access={access} "
            f"address={'' if d.target is None else format(d.target, 'x')}\n")


def _crash(text: str, t: Trace) -> Crash | None:
    m = _DUMP_MARK.search(text)
    if m:                                     # the game's own crash dump (written by its crash handler)
        code = m.group(1).lower()
        c = Crash("", code, _FATAL.get(code, f"exception {code}"), module=m.group(2), offset=int(m.group(3), 16),
                  access=m.group(4), address=int(m.group(5), 16) if m.group(5) else None, at=m.start(), certain=True)
        ld = next((x for x in t.loads if x.name == c.module.lower()), None)
        c.kind = ld.kind if ld else ("native" if c.module else "")
        if t.game_tids:
            c.tid = sorted(t.game_tids)[0]
        return c
    m = _UNHANDLED.search(text)
    if m:                                     # Wine's own "nothing caught it" (winedbg's report)
        c = Crash("", m.group(1).lower(), _FATAL.get(m.group(1).lower(), f"exception {m.group(1)}"), at=m.start(),
                  certain=True)
        c.address = None
        _locate(c, int(m.group(2), 16), text, t)
        return c
    best = None
    for m in _EXC.finditer(text):
        tid, code = m.group(1), m.group(2).lower()
        if code in _BENIGN or (t.game_tids and tid not in t.game_tids and not _game_thread(tid, text, t)):
            continue
        if code not in _FATAL:
            continue
        after = text[m.end():m.end() + 20000]
        own = "\n".join(ln for ln in after.splitlines()[:400] if ln.startswith(tid + ":"))
        certain = bool(_LAST_CHANCE.search(own))
        c = Crash(tid, code, _FATAL[code], at=m.start(), certain=certain)
        info = re.search(r"info\[0\]=0*([0-9a-f]+)\s*\n[^\n]*info\[1\]=([0-9A-Fa-f]+)", after[:600])
        if info and code == "c0000005":
            c.access = {"0": "read", "1": "write", "8": "execute"}.get(info.group(1), "read")
            c.address = int(info.group(2), 16)
        _locate(c, int(m.group(3), 16), own or after, t)
        if certain and (best is None or not best.certain):
            best = c                          # the first one nothing caught (later ones: inside the crash reporter)
        elif best is None or not best.certain:
            best = c if (t.exit_code not in (None, 0) and not t.ran) else best
    return best


def _game_thread(tid: str, text: str, t: Trace) -> bool:
    """A thread whose backtraces run through the game's own exe belongs to the game's process."""
    exe = Path(t.exe).name.lower()
    return any(m.group(1) == tid and (m.group(2).lower().endswith(exe) or "shipping" in m.group(2).lower())
               for m in _BACKTRACE.finditer(text[:2_000_000]))


def _locate(c: Crash, addr: int, own: str, t: Trace) -> None:
    bt = _BACKTRACE.search(own)
    if bt:                                    # Wine names the module and offset itself
        c.module = bt.group(2).replace("\\\\", "\\").split("\\")[-1]
        c.offset = int(bt.group(3), 16)
        ld = next((x for x in t.loads if x.name == c.module.lower()), None)
        c.kind = ld.kind if ld else ""
        return
    mods = sorted((ld for ld in t.loads if not t.game_tids or ld.tid in t.game_tids), key=lambda x: x.base)
    best = None
    for ld in mods:
        if ld.base <= addr:
            best = ld
        else:
            break
    if best is not None:
        c.module, c.offset, c.kind = best.path.split("\\")[-1], addr - best.base, best.kind


# Wine's own plumbing: loaded by everything, tells nothing about what the game was doing
_PLUMBING = re.compile(r"^(api-ms-|ext-ms-|mmdevapi|win(epulse|ealsa|ex11|ewayland)|psapi|msv1_0|kerberos|secur32|"
                       r"sechost|rpcrt4|imm32|uxtheme|comctl32|winspool|wbemprox|msctf|propsys|bcrypt|crypt32|"
                       r"cryptbase|ntdll|kernel|ucrtbase|msvcrt|advapi32|user32|gdi32|win32u|ole32|oleaut32|shell32)",
                       re.I)


def _module_at_addr(t: Trace, tid: str, addr: int) -> tuple[str, int, str]:
    """(module name, offset, full path) of the code at ``addr`` in the process of thread ``tid``."""
    mods = sorted(t.loads, key=lambda ld: ld.base)
    best = None
    for ld in mods:
        if ld.base <= addr:
            best = ld
        else:
            break
    if best is None or addr - best.base > 0x20000000:
        return "", 0, ""
    return best.path.split("\\")[-1], addr - best.base, best.path


def _calls(text: str, t: Trace, dirs: list[Path]) -> None:
    """The traced calls: which module ended a process (and with what code), which started another program."""
    seen_tid_exit: set[str] = set()
    for m in _RELAY_CALL.finditer(text):
        tid, dll, func, args, ret = m.group(1), m.group(2), m.group(3), m.group(4), int(m.group(5), 16)
        f = func.lower()
        if f not in _EXIT_FUNCS and f not in _LAUNCH_FUNCS:
            continue
        mod, off, path = _module_at_addr(t, tid, ret)
        ours = (t.game_tids and tid in t.game_tids) or (path and _under(_win_dir(path), Path(t.exe).parent))
        if not ours:
            continue
        if f in _EXIT_FUNCS:
            nums = [x.strip() for x in args.split(",") if x.strip()]
            handle, code = None, 0
            try:
                if f in ("terminateprocess", "ntterminateprocess"):
                    handle = int(nums[0], 16) if nums else None
                    code = int(nums[1], 16) & 0xFFFFFFFF if len(nums) > 1 else 0
                elif nums:
                    code = int(nums[0], 16) & 0xFFFFFFFF
            except ValueError:
                pass
            own = handle is None or handle in (0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF, 0)
            if own and tid in seen_tid_exit:
                continue                       # (exit → ExitProcess → RtlExitUserProcess: the first says who)
            if own:
                seen_tid_exit.add(tid)
            t.exits.append({"module": mod, "offset": off, "func": func, "code": code, "self": own, "tid": tid,
                            "at": m.start()})
        else:
            strings = [x.replace("\\\\", "\\") for x in _WSTR.findall(args)]
            target = next((x for x in strings if x and not x.lower() in ("open", "runas")), "")
            t.launches.append({"module": mod, "offset": off, "func": func, "target": target[:300], "tid": tid,
                               "at": m.start()})
    for m in _RELAY_RET.finditer(text):           # did the start work? (CreateProcess: 0 = failed)
        la = next((x for x in t.launches if x["tid"] == m.group(1) and x["func"] == m.group(3)
                   and x["at"] < m.start() and "ok" not in x), None)
        if la is not None:
            val = int(m.group(4), 16)
            la["ok"] = val != 0 if m.group(3).startswith("CreateProcess") else val > 32
    t.exits = t.exits[:8]
    t.launches = t.launches[:8]


def _game_logs(extra: str, t: Trace) -> None:
    """Error lines from the logs the game wrote during the run (``ezgd-gamelog:`` blocks), the last ones first."""
    from ezgd.library.screenread import STRONG, error_lines
    if _GAMELOG not in (extra or ""):
        return
    out: list[tuple[str, str]] = []
    for block in extra.split(_GAMELOG)[1:]:
        name, _, body = block.partition("\n")
        tail = "\n".join(body.splitlines()[-40:])            # why it stopped is at the end of its log
        lines = [ln for ln in error_lines(tail, limit=40) if STRONG.search(ln)]
        for ln in lines[-2:]:
            out.append((name.strip().replace("\\", "/").rsplit("/", 1)[-1], ln))
    t.game_errors = out[:6]


def _setup(text: str) -> dict:
    import json
    from ezgd.library.history import SETUP_MARK
    i = text.find(SETUP_MARK)
    if i < 0:
        return {}
    try:
        return json.loads(text[i + len(SETUP_MARK):text.find("\n", i)])
    except ValueError:
        return {}


def _lookups(text: str, t: Trace, dirs: list[Path]) -> None:
    """Files and registry keys the game's process looked for and did not find (noise — Wine's own probing, DLL
    search paths, fonts — left out)."""
    game = t.game_tids
    last = None
    for m in _FILE_MISS.finditer(text):
        tid, path = m.group(1), (m.group(2) or m.group(3)).replace("\\\\", "\\")
        if (game and tid not in game) or _FILE_NOISE.search(path) or (tid, path) == last:
            continue
        last = (tid, path)
        t.files_missing.append((tid, path, m.start()))
    if "ezgd:keys-traced" in text:                  # a condensed closer look: the misses are marked already
        t.keys_traced = True
        for m in _KEY_MARK.finditer(text):
            tid, path = m.group(1), _key_path(m.group(2).replace("\\\\", "\\"))
            if (game and tid not in game) or path is None or _KEY_NOISE.search(path):
                continue
            if not any(k == path for _t, k, _a in t.keys_missing):
                t.keys_missing.append((tid, path, m.start()))
    pending: dict[str, tuple[str, int]] = {}
    for m in _REG.finditer(text):
        t.keys_traced = True
        tid = m.group(1)
        if m.group(3) is not None:
            pending[tid] = (m.group(3).replace("\\\\", "\\"), m.start())
            continue
        key = pending.pop(tid, None)
        if key is None or m.group(4) != "(nil)" or (game and tid not in game):
            continue
        path = _key_path(key[0])
        if path is None or _KEY_NOISE.search(path):
            continue
        if not any(k == path for _t, k, _a in t.keys_missing):
            t.keys_missing.append((tid, path, key[1]))


def _key_path(key: str) -> str | None:
    """'\\Registry\\Machine\\Software\\X' / 'Software\\X' → 'Software\\X'; None for a path relative to another open
    key (Wine's own lookups: 'AppDefaults\\game.exe' …) — a game's own key is always looked up from Software."""
    k = re.sub(r"^\\?registry\\(machine|user\\[^\\]+)\\", "", key.strip("\\"), flags=re.I)
    return k if k.lower().startswith("software\\") else None


# files games look for and may well not have (caches, locale data, crash reporters, editor-only files, GPU vendor
# probes): not a cause — Expedition 33's closer look listed 160 of them, all harmless
_OPTIONAL = re.compile(r"(\\$|pipelinecache|\\pipelinecaches\\|\\icudt\d+|\.sentry-native|\\crashes\\|crashreport|"
                       r"\\intermediate\\|shaderworkingdir|\\shaders?\\|shadercache|\\nvidia\\ngx\\|\\openvr\\|"
                       r"\\editor\\|editorkeybindings|persistentdownloaddir|\\saved\\(logs|crashes|config)\\|dxvk\.conf|"
                       r"vkd3d-proton|\.(cur|res|cnv|vrpath|log|bak|old|tmp)$|\\temp\\|binaryconfig\.ini|"
                       r"\\steam_settings\\|\\local\\(nvidia|amd|intel)|\\cache\\|\.so(\.\d+)*$|"
                       r"\\pressure-vessel\\|\\compatibilitytools\.d\\|\\steamapps\\common\\proton)", re.I)


def missing_before_end(t: Trace, n: int = 4, harmless: set[str] | None = None) -> tuple[list[str], list[str]]:
    """The last files and keys the game looked for in vain before it crashed (or before the run ended) — on the
    thread that crashed, not the optional ones every game probes, and not ones that were missing in a run that
    worked too (``harmless``)."""
    end = t.crash.at if t.crash is not None else 1 << 62
    tid = t.crash.tid if t.crash is not None and t.crash.tid else None
    harmless = {h.lower() for h in harmless or ()}
    files = [p for th, p, at in t.files_missing if at < end and (tid is None or th == tid)
             and not _OPTIONAL.search(p) and p.lower() not in harmless][-n:]
    keys = [k for th, k, at in t.keys_missing if at < end and (tid is None or th == tid)][-n:]
    return files, keys


def release_reg_files(exe: Path, limit: int = 20) -> list[Path]:
    """.reg files the release ships (what its installer would have imported)."""
    from ezgd.platform.proton import game_binary_dirs
    dirs = game_binary_dirs(exe)
    root = dirs[0]
    found = []
    for pattern in ("*.reg", "*/*.reg", "*/*/*.reg"):
        found += [p for p in root.glob(pattern) if p.is_file()]
    for d in dirs[1:]:
        found += [p for p in d.glob("*.reg") if p.is_file()]
    return list(dict.fromkeys(found))[:limit]


def _reg_text(p: Path) -> str:
    raw = p.read_bytes()[:2_000_000]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8", "replace")


def _reg_has(reg: Path, key: str) -> bool:
    """Does a .reg file create this key (matched by its path below the hive: Software\\Vendor\\Game)?"""
    try:
        text = _reg_text(reg).lower()
    except OSError:
        return False
    k = key.lower().strip("\\")
    k = re.sub(r"^\\?registry\\(machine|user\\[^\\]+)\\", "", k)
    return any(sect.endswith(k) or sect.endswith(k.replace("software\\", "software\\wow6432node\\", 1))
               for sect in re.findall(r"^\[([^\]\r\n]+)\]", text, re.M))


def _find_names(root: Path, names: set[str], limit: int = 60_000) -> dict[str, Path]:
    """Where files with these names are in the game's folder (one walk, capped: a game can hold 100 000 files)."""
    import os
    out: dict[str, Path] = {}
    seen = 0
    for d, _sub, fs in os.walk(root):
        for f in fs:
            seen += 1
            if f.lower() in names and f.lower() not in out:
                out[f.lower()] = Path(d) / f
        if seen > limit or len(out) == len(names):
            break
    return out


def just_before(t: Trace, n: int = 3) -> list[str]:
    """The last DLLs the game loaded before it crashed (what it was doing)."""
    if t.crash is None:
        return []
    game = [ld for ld in t.loads if (not t.game_tids or ld.tid in t.game_tids) and ld.at < t.crash.at]
    out: list[str] = []
    for ld in reversed(game):
        if ld.name.endswith(".drv") or _PLUMBING.match(ld.name):
            continue
        if ld.name not in out:
            out.append(ld.name)
        if len(out) >= n:
            break
    return list(reversed(out))


# ------------------------------------------------------------------------- reasoning
@dataclass
class Suspect:
    key: str                   # stable id ("loader:winmm.dll"), so a step is not tested twice
    title: str
    why: str                   # the reasoning that leads here
    confidence: float
    fix: object = None         # fixgen.GeneratedFix (its overrides are tested), or None for information only
    expect: dict = field(default_factory=dict)   # what the next run must show if this is right
    overrides: dict | None = None                # (a suspect from the regular checks: its test, ready made)

    def changes(self, entry) -> dict:
        """The launch-plan overrides that test this suspect on top of the game's current setup."""
        if self.overrides is not None:
            return self.overrides
        return self.fix.overrides(entry) if self.fix is not None else {}

    @property
    def gen_id(self) -> str | None:
        return getattr(self.fix, "gen_id", None)

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "why": self.why, "confidence": self.confidence,
                "expect": self.expect, "fix": self.fix.to_dict() if self.fix is not None else None}


def follow(t: Trace, entry=None, good: dict | None = None) -> tuple[list[str], list[Suspect]]:
    """Ask "why?" from how the run ended backwards. Returns (the chain of reasoning, the suspects it points at —
    most likely first). ``good``: the summary of a run that worked (what was missing then is not a cause)."""
    from ezgd.library.fixgen import GeneratedFix
    chain: list[str] = []
    out: list[Suspect] = []
    # 1. how it ended
    if t.errors:
        chain.append(f"It stopped on an error message: “{t.errors[0][:200]}”.")
    elif t.crash is not None:
        when = f" {t.seconds}s after it started" if t.seconds else ""
        chain.append(f"It crashed{when}: {t.crash.describe()}"
                     + ("" if t.crash.certain else " (the last serious exception before it quit)") + ".")
    elif t.ran:
        chain.append("It kept running until the test ended.")
    elif t.exit_code is not None:
        chain.append(f"It quit by itself with exit code {t.exit_code} after {t.seconds}s, without an error message "
                     "or a crash.")
    # 1b. what the system logged: the kernel killed it, or the graphics driver faulted under it
    for ev in t.serious()[:2]:
        chain.append(_system_line(ev))
    if not t.system_readable and quiet_end(t):
        chain.append("The system log could not be read on this computer, so a kill for lack of memory or a graphics "
                     "driver fault would not show here.")
    out += _system_suspects(t)
    if t.phase:
        chain.append("How far it got: " + " → ".join(t.phase) + ".")
    else:
        chain.append("It did not even get as far as Proton's start-up messages.")
    before = just_before(t)
    if before:
        chain.append("Just before that it loaded " + ", ".join(before) + ".")
    # 2. the game's own error message names the cause
    for msg in t.errors[:2]:
        key, what, fx = errorbox.explain(msg)
        if fx is None:
            continue
        kind, value, label = fx
        g = GeneratedFix(f"trace:box:{key}", label, f"The game said: “{msg[:200]}” — {what}.")
        if kind == "winetricks":
            g.winetricks = value.split()
        elif kind == "add_args":
            g.args = value
        else:
            continue
        out.append(Suspect(f"box:{key}", label, f"The game's own message names it: {what}.", 0.9, g,
                           {"no_error": msg}))
    # 3. a DLL the game needs is not there
    for dll, needer in t.missing[:2]:
        from ezgd.library.diagnose import _runtime_for
        rt = _runtime_for(dll)
        chain.append(f"{needer} needs {dll}, and it was not found.")
        if rt and rt[0]:
            verb, what = rt
            g = GeneratedFix(f"trace:missing:{dll}", f"Install {what}", f"{needer} needs {dll} ({what}).",
                             winetricks=verb.split())
            out.append(Suspect(f"missing:{dll}", f"Install {what}", f"{dll} comes with {what}.", 0.85, g,
                               {"loads": [dll]}))
    # 4. the game's own DLLs replaced by Wine's: add-on loaders that never ran
    if t.replaced:
        names = [r.name for r in t.replaced]
        chain.append("The game ships its own " + ", ".join(names) + ", but Wine loaded its built-in "
                     + ("copy" if len(names) == 1 else "copies") + " instead.")
        known = [r for r in t.replaced if r.loader]
        for r in known:
            line = f"{r.name} is {r.loader}'s loader"
            if r.payload_missing:
                line += " — so " + ", ".join(r.payload_missing) + " never loaded"
            chain.append(line + ".")
        stuck = not t.ran or "game loaded" not in t.phase
        conf = 0.92 if any(r.payload_missing for r in known) else 0.8 if known else 0.55 if stuck else 0.3
        payloads = sorted({p for r in known for p in r.payload_missing if not p.startswith(".")})
        g = GeneratedFix("trace:game-dlls", "Load the game's own " + ", ".join(names),
                         "Wine used its own " + ", ".join(names) + " instead of the game's"
                         + (" — " + "; ".join(f"{r.name} loads {r.loader}" for r in known) if known else "") + ".",
                         dll_overrides={n[:-4]: "n,b" for n in names})
        why = ("Without " + ", ".join(r.loader for r in known) + " the release cannot work as intended"
               if known else "Wine's built-in copies skip whatever the game's own copies add")
        out.append(Suspect("game-dlls:" + ",".join(names), g.label, why + ".", conf, g,
                           {"native": names, "loads": payloads}))
    # 5. it crashed inside an add-on that did load (a mod, ReShade, an overlay): without it, does it start?
    c = t.crash
    if c is not None and c.module and c.kind == "native":
        mod = c.module.lower()
        addon = next((ld for ld in t.loads if ld.name == mod and _addon(ld, t)), None)
        if addon is not None and mod.endswith(".dll"):
            stem = mod[:-4]
            g = GeneratedFix(f"trace:without:{mod}", f"Start without {c.module}",
                             f"The crash is inside {c.module}, an add-on in the game's folder, not the game itself.",
                             dll_overrides={stem: "b"} if _is_wine_dll(stem) else {stem: ""})
            chain.append(f"{c.module} is not part of the game — an add-on in its folder.")
            out.append(Suspect(f"without:{mod}", g.label, "The crash site is the add-on itself.", 0.7, g,
                               {"crash_moved": mod}))
    # 6. Wine itself lacks a function the game calls: only another Proton can help
    for fn in t.unimplemented[:1]:
        chain.append(f"The game called {fn}, which this Proton's Wine does not implement.")
    # 7. it just quit: who decided to (a closer look traces it), what the game's own logs and Steam's API said
    quiet = t.crash is None and not t.errors and not t.ran
    failed_run = not t.ran and (t.crash is not None or t.errors or (t.exit_code not in (None, 0)) or quiet)
    for msg in t.steam_fail[:2] if failed_run else []:
        chain.append(f"Steam's API said: “{msg}”.")
    harmless = {ln for _f, ln in ((good or {}).get("game_errors") or [])}
    for fname, line in [x for x in t.game_errors if x[1] not in harmless][:2] if failed_run else []:
        chain.append(f"The last error-like line in its own log ({fname}): “{line[:200]}”.")
        key, what, fx = errorbox.explain(line)
        if fx is not None and fx[0] in ("winetricks", "add_args"):
            g = GeneratedFix(f"trace:log:{key}", fx[2], f"The game's log says: “{line[:200]}” — {what}.")
            if fx[0] == "winetricks":
                g.winetricks = fx[1].split()
            else:
                g.args = fx[1]
            out.append(Suspect(f"log:{key}", fx[2], f"Its own log names it: {what}.", 0.8, g))
    launched_then_quit = []
    for la in t.launches[:3]:
        who = la["module"] or "the game"
        if la["target"].lower().startswith("steam://"):
            chain.append(f"{who} asked Steam to start it ({la['target'][:80]}) — this release wants Steam to "
                         "launch it.")
        elif la["target"] and la.get("ok") is False:
            chain.append(f"{who} tried to start {la['target'][:160]}, and that failed — the file was not found "
                         "where it looked (a launcher started from another folder, or a file missing).")
            launched_then_quit.append(la)
        elif la["target"]:
            chain.append(f"{who} started {la['target'][:160]}.")
            if any(x["self"] and x["tid"] == la["tid"] for x in t.exits):
                launched_then_quit.append(la)
    if quiet:
        for x in [x for x in t.exits if x["self"] and x["module"]][:3]:
            mod = x["module"].lower()
            started = next((la for la in launched_then_quit if la["tid"] == x["tid"]), None)
            if started is not None:
                chain.append(f"{x['module']} quit ({x['func']}({x['code']})) right after "
                             + ("failing to start " if started.get("ok") is False else "starting ")
                             + f"{started['target'][:120]} — it is a launcher.")
                continue
            line = f"{x['module']} ended the game — it called {x['func']}({x['code']})"
            if mod.startswith("steam_api"):
                line += (": Steam's API ends a game when Steam is not running for it, or when the game asks Steam "
                         "to start it instead (SteamAPI_RestartAppIfNecessary)")
            elif mod.startswith("onlinefix"):
                line += (": OnlineFix does that when Steam is not running, or when the fix does not match this "
                         "version of the game")
            elif mod.startswith(("eossdk", "galaxy")):
                line += ": the store's own SDK (Epic / GOG) refused to start"
            elif mod.lower() == Path(t.exe).name.lower() or mod.endswith("-shipping.exe"):
                line += ": the game itself decided to quit" + (" (its log says why, above)" if t.game_errors else "")
            chain.append(line + ".")
    for la in launched_then_quit:              # a launcher: started the real game and quit — start that directly
        name = la["target"].replace('"', "").split("\\")[-1].split(" ")[0]
        if not name.lower().endswith(".exe") or name.lower() == Path(t.exe).name.lower():
            continue
        found = _find_names(Path(t.exe).parent, {name.lower()})
        target = found.get(name.lower())
        if target is not None:
            out.append(Suspect(f"launcher:{name.lower()}", f"Start {name} directly",
                               f"{Path(t.exe).name} is a launcher: it " + ("could not start" if la.get("ok") is False
                                                                          else "starts")
                               + f" {name} and quits.", 0.85 if la.get("ok") is False else 0.8,
                               overrides={"executable": str(target)}))
    # 8. what it looked for, just before it died, and did not find
    failed = (t.crash is not None or t.errors or (t.exit_code not in (None, 0) and not t.ran))
    files, keys = missing_before_end(t, harmless=set((good or {}).get("missing") or []))
    if failed and files:
        root = Path(t.exe).parent
        where = _find_names(root, {f.rsplit("\\", 1)[-1].lower() for f in files})
        shown = []
        for f in files:
            elsewhere = where.get(f.rsplit("\\", 1)[-1].lower())
            shown.append(f + (f" (a file with that name is at {elsewhere.relative_to(root)})" if elsewhere else ""))
        chain.append("Just before it " + ("crashed" if t.crash is not None else "stopped")
                     + " it looked for files that are not there: " + "; ".join(shown) + ".")
    if failed and keys:
        chain.append("It also looked for registry keys that are not there: " + "; ".join(keys)
                     + " — a Windows installer would have created them.")
        regs = release_reg_files(Path(t.exe))
        for reg in regs:
            covers = [k for k in keys if _reg_has(reg, k)]
            if covers or (len(regs) == 1 and keys):
                g = GeneratedFix(f"trace:reg:{reg.name.lower()}", f"Import the release's {reg.name}",
                                 f"The game looked for {', '.join(covers or keys)}; the release ships {reg.name}, "
                                 "which its installer would have imported.")
                out.append(Suspect(f"reg:{reg.name.lower()}", g.label,
                                   (f"{reg.name} creates exactly {covers[0]}" if covers else
                                    f"{reg.name} is the release's registry file"), 0.88 if covers else 0.5,
                                   g, {"no_key": covers or keys}, overrides={"registry": [str(reg)]}))
    elif failed and not t.keys_traced and not out:
        chain.append("Registry lookups were not recorded in this run — a closer look records them.")
    out.sort(key=lambda s: -s.confidence)
    return chain, out


def quiet_end(t: Trace) -> bool:
    """It ended without saying why — no error message, no crash Wine saw, not still running."""
    return not t.ran and not t.errors and t.crash is None


_GPU_LIBS = ("libnvidia", "libcuda", "libvulkan_", "libgallium", "radeonsi", "iris_dri", "libdrm", "libGLX_",
             "libEGL_nvidia", "nvidia_drv")


def _system_line(ev) -> str:
    from ezgd.library import syslog
    if ev.kind in ("oom", "oomd"):
        mem = syslog.memory_facts()
        have = f" of {mem['MemTotal'] / (1 << 30):.0f} GB" if mem.get("MemTotal") else ""
        swap = "" if not mem else " This computer has no swap space." if not mem.get("SwapTotal") else \
            f" Swap: {mem['SwapTotal'] / (1 << 30):.0f} GB."
        used = f" — the game was using {ev.mem / (1 << 30):.1f} GB{have}" if ev.mem else ""
        who = "The kernel" if ev.kind == "oom" else "systemd-oomd"
        return f"{who} killed it because the computer ran out of memory{used}.{swap}"
    if ev.kind == "gpu":
        whose = syslog.XID.get(int(ev.code.split(":")[1]), ("", ""))[1] if ev.code.startswith("nvidia:") else ""
        line = f"The graphics driver reported a GPU fault during the run ({ev.code}: {ev.detail})"
        if whose == "hardware":
            line += " — that points at the hardware (power, the PCIe slot, the card's memory), not at the game"
        elif whose == "game":
            line += " — the GPU failed on the game's own commands (how the game's graphics are translated, or a " \
                    "driver bug they trigger)"
        return line + "."
    where = f" in {ev.lib}" if ev.lib else ""
    line = f"It crashed outside Wine's control: {ev.proc} — {ev.detail}{where}"
    if ev.lib.startswith(_GPU_LIBS):
        line += " (inside the graphics driver)"
    return line + "."


def _system_suspects(t: Trace) -> list[Suspect]:
    """What the kernel's or the driver's account points at. A kill for memory has no launch setting that fixes it —
    it is said plainly (information); a GPU fault on the game's work has settings worth testing, each predicted to
    make the fault disappear."""
    from ezgd.library import syslog
    from ezgd.library.fixgen import GeneratedFix
    out: list[Suspect] = []
    events = t.serious()
    if any(e.kind in ("oom", "oomd") for e in events):
        mem = syslog.memory_facts()
        steps = ["close other programs while playing (a browser can hold several GB)",
                 "lower the game's texture quality"]
        if mem and not mem.get("SwapTotal"):
            steps.insert(0, "add swap space or zram — this computer has none, so the kernel had nothing to fall "
                            "back on")
        out.append(Suspect("system:memory", "The computer ran out of memory",
                           "Linux stopped the game to keep the computer running. To avoid it: " + "; ".join(steps)
                           + ".", 0.9))
    gpu = [e for e in events if e.kind == "gpu"]
    if gpu:
        e = gpu[0]
        hardware = e.code.startswith("nvidia:") and \
            syslog.XID.get(int(e.code.split(":")[1]), ("", ""))[1] == "hardware"
        if hardware:
            out.append(Suspect("system:gpu-hardware", "Check the graphics card's power and connection",
                               f"The driver's report ({e.code}: {e.detail}) is a hardware fault: the power cable, the "
                               "power supply under load, or the card's seating in its slot.", 0.8))
        else:
            expect = {"no_system": "gpu"}
            if e.code.startswith("nvidia:") and (t.loaded("nvapi64.dll") or t.loaded("nvngx_dlss.dll")):
                out.append(Suspect("system:no-nvapi", "Turn NVAPI off (DLSS / Reflex)",
                                   "The GPU faulted on an NVIDIA card while the game used NVAPI (DLSS / Reflex); "
                                   "without it the game takes its plain rendering path.", 0.5,
                                   GeneratedFix("sys:no-nvapi", "Turn NVAPI off (DLSS / Reflex)",
                                                f"The graphics driver reported {e.code} while NVAPI was in use.",
                                                options=["no_nvapi"]), expect))
            shipping = any(n.endswith("-shipping.exe") for n in {ld.name for ld in t.loads})
            if t.loaded("d3d12.dll") and shipping:
                out.append(Suspect("system:dx11", "Start it with DirectX 11 (-dx11)",
                                   "The GPU faulted under the game's DirectX 12 renderer; Unreal games can draw with "
                                   "DirectX 11 instead (translated by DXVK, a different path).", 0.55,
                                   GeneratedFix("sys:dx11", "Start it with DirectX 11 (-dx11)",
                                                f"The graphics driver reported {e.code} under DirectX 12.",
                                                args="-dx11"), expect))
    return out


def _addon(ld: Load, t: Trace) -> bool:
    """A DLL in the game's folder that is not the game's or its engine's (a mod, an injected overlay, a fix)."""
    name = ld.name
    engine = ("ue4", "ue5", "unreal", "unity", "mono", "steam_api", "eos", "physx", "nvngx", "amd_", "ffx", "bink",
              "fmod", "wwise", "tbb", "openimage", "opencolor", "oo2", "libcef", "d3dcompiler", "gfsdk")
    if name.startswith(engine):
        return False
    return any(name[:-4] in lo["proxies"] or name in lo["payload"] for lo in LOADERS) or name.endswith(".asi")


def _is_wine_dll(stem: str) -> bool:
    return stem in {"winmm", "version", "winhttp", "dinput8", "dsound", "dxgi", "d3d9", "d3d11", "d3d12", "opengl32",
                    "dwmapi", "xinput1_3", "xinput1_4", "iphlpapi", "wininet", "dbghelp"}


# ------------------------------------------------------------------------- judging a test
def verdict(before: Trace, after: Trace, expect: dict) -> tuple[str, str]:
    """('fixed' | 'progress' | 'confirmed' | 'no-change' | 'worse', why). ``confirmed``: the prediction came true
    (the suspect was right) even though the game still fails — keep it and follow the next error."""
    if after.ran and "game loaded" in after.phase:
        return "fixed", "it got into the game and kept running"
    if after.ran and not before.ran:
        return "fixed", "it kept running until the test ended"
    met, missed = [], []
    for name in expect.get("native", []):
        (met if after.loaded_native(name) else missed).append(f"the game's own {name} loaded")
    for name in expect.get("loads", []):
        (met if after.loaded(name) else missed).append(f"{name} loaded")
    if "no_error" in expect:
        (missed if expect["no_error"] in after.errors else met).append("the error message is gone")
    if "no_key" in expect:
        still = {k.lower() for _t, k, _a in after.keys_missing}
        if after.keys_traced:
            (missed if any(k.lower() in still for k in expect["no_key"]) else met).append("the registry key is found now")
    if "no_system" in expect:
        kind = expect["no_system"]
        (missed if any(e.kind == kind for e in after.serious()) else met).append(
            {"gpu": "no GPU fault any more", "oom": "not killed for memory any more"}.get(kind, f"no {kind} any more"))
    if "crash_moved" in expect and before.crash is not None:
        moved = after.crash is None or after.crash.module.lower() != expect["crash_moved"]
        (met if moved else missed).append(f"no crash in {expect['crash_moved']} any more")
    if after.score() < before.score():
        return "worse", "it got less far than before (" + after.first_problem() + ")"
    if met and not missed:
        return "confirmed", "as predicted: " + ", ".join(met) + " — next problem: " + after.first_problem()
    if after.score() > before.score():
        return "progress", "it got further (" + (" → ".join(after.phase) or "longer") + ") — next problem: " + \
            after.first_problem()
    if after.first_problem() != before.first_problem() and met:
        return "confirmed", "partly as predicted: " + ", ".join(met) + " — now: " + after.first_problem()
    return "no-change", (("not as predicted: " + ", ".join(missed)) if missed else "the same problem") + \
        f" ({after.first_problem()})"


# ------------------------------------------------------------------------- memory of solved crashes
def _is_game_module(t: Trace, module: str) -> bool:
    from ezgd.platform.proton import game_binary_dirs
    m = module.lower()
    if m == Path(t.exe).name.lower() or m.endswith("-shipping.exe"):
        return True
    return any((d / module).is_file() for d in game_binary_dirs(Path(t.exe)))


def fingerprint(t: Trace, engine: str | None = None) -> dict:
    """What identifies this failure, independent of the game where it happened (the build of the game's own
    module is kept, so the very same crash of the very same release is recognised exactly)."""
    sig: dict = {"engine": engine or ""}
    c = t.crash
    if c is not None:
        own = bool(c.module) and _is_game_module(t, c.module)
        sig.update(code=c.code, module="<game>" if own else c.module.lower(), null=c.null)
        if own and c.module:
            from ezgd.platform.proton import game_binary_dirs
            f = next((d / c.module for d in game_binary_dirs(Path(t.exe)) if (d / c.module).is_file()), None)
            try:
                sig["build"] = f"{c.module.lower()}:{f.stat().st_size}:{c.offset:x}" if f else ""
            except OSError:
                sig["build"] = ""
    sig["replaced"] = sorted(r.name for r in t.replaced)
    sig["loaders"] = sorted({r.loader for r in t.replaced if r.loader})
    sig["box"] = errorbox.explain(t.errors[0])[0] if t.errors else ""
    sig["missing"] = sorted({d for d, _n in t.missing})
    sig["keys"] = sorted({k.lower() for _t, k, _a in t.keys_missing})[:5]
    sys_ = sorted({e.kind if e.kind != "gpu" else e.code for e in t.serious()})
    if sys_:
        sig["system"] = sys_
    return sig


def _sig_id(sig: dict) -> str:
    import hashlib
    import json
    core = {k: sig.get(k) for k in ("code", "module", "null", "build", "replaced", "box", "missing")}
    if sig.get("system"):                      # (only when present: the ids of failures kept before stay the same)
        core["system"] = sig["system"]
    return hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]


def _similar(a: dict, b: dict) -> tuple[float, str]:
    """How sure that two fingerprints are the same problem, and why."""
    if a.get("build") and a.get("build") == b.get("build") and a.get("code") == b.get("code"):
        return 0.95, "the very same crash (same game build, same place in its code)"
    if a.get("replaced") and a.get("replaced") == b.get("replaced") and a.get("loaders") == b.get("loaders"):
        return 0.88, "the game's own " + ", ".join(a["replaced"]) + " were replaced by Wine's, as there"
    if a.get("box") and a.get("box") not in ("other", "") and a.get("box") == b.get("box"):
        return 0.85, "the same error message"
    if a.get("missing") and a.get("missing") == b.get("missing"):
        return 0.85, "the same missing " + ", ".join(a["missing"])
    if a.get("keys") and a.get("keys") == b.get("keys"):
        return 0.8, "the same missing registry keys"
    if a.get("module") and a.get("module") != "<game>" and a.get("module") == b.get("module") and \
            a.get("code") == b.get("code") and a.get("engine") == b.get("engine"):
        return 0.7, f"a crash in {a['module']} in a game with the same engine"
    gpu = [x for x in a.get("system") or [] if ":" in x]
    if gpu and gpu == [x for x in b.get("system") or [] if ":" in x] and a.get("engine") == b.get("engine"):
        return 0.6, f"the same GPU fault ({', '.join(gpu)}) in a game with the same engine"
    return 0.0, ""


def _load_memory(path: Path) -> dict:
    import json
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict):
            return {"solved": list(data.get("solved") or []), "no_help": dict(data.get("no_help") or {})}
    except (OSError, ValueError):
        pass
    return {"solved": [], "no_help": {}}


def _save_memory(path: Path, data: dict) -> None:
    import json
    data["solved"] = data["solved"][-300:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), "utf-8")
    tmp.replace(path)


def _this_machine() -> dict:
    try:
        from ezgd.platform import machine
        return machine.profile()
    except Exception:
        return {}


def remember(path: Path, t: Trace, engine: str | None, game: str, fixes: list[dict], overrides: dict,
             labels: list[str], machine: dict | None = None) -> None:
    """A failure that was solved: its fingerprint, what solved it (``fixes``: GeneratedFix dicts that rebuild
    the change for any game; ``overrides``: the exact change, reused only for the same game) and the computer it
    happened on (the same-looking failure can have another cause elsewhere)."""
    import time as _time
    data = _load_memory(path)
    sig = fingerprint(t, engine)
    data["solved"] = [r for r in data["solved"] if not (r.get("game") == game and _sig_id(r.get("sig", {})) ==
                                                        _sig_id(sig))]
    data["solved"].append({"sig": sig, "game": game, "fixes": fixes, "overrides": overrides, "labels": labels,
                           "at": _time.time(), "machine": machine if machine is not None else _this_machine()})
    _save_memory(path, data)


# modules that belong to one GPU vendor: a crash in them says nothing about a computer with another vendor's card
_VENDOR_MODULES = {"nvidia": re.compile(r"^(nvapi|nvwgf|nvoglv|nvd3dum|nvcuda|nvngx|_nvngx)", re.I),
                   "amd": re.compile(r"^(amdxc|atidxx|atiu|amdvlk|amd_ags|aticfx)", re.I),
                   "intel": re.compile(r"^(igd|igxe|intel)", re.I)}


def remember_no_help(path: Path, t: Trace, engine: str | None, game: str, keys: list[str]) -> None:
    """Suspects tested for this failure of this game that changed nothing — not tested again next time."""
    if not keys:
        return
    data = _load_memory(path)
    k = f"{game}|{_sig_id(fingerprint(t, engine))}"
    data["no_help"][k] = sorted(set(data["no_help"].get(k, [])) | set(keys))[-40:]
    _save_memory(path, data)


def no_help(path: Path, t: Trace, engine: str | None, game: str) -> set[str]:
    return set(_load_memory(path)["no_help"].get(f"{game}|{_sig_id(fingerprint(t, engine))}", []))


# ------------------------------------------------------------- what other computers shared (untrusted input)
# A record from another computer is rebuilt from what is allowed below, never used as it came: nobody who shares
# knowledge must be able to make this computer run anything. (The server's signature — crowd.py — already rejects
# anything it did not publish; this also holds against poisoned reports the server accepted.)
# Not shared at all: raw launch-option lines and game arguments with shell characters (launch options run through
# /bin/sh), file edits (absolute paths on the other computer — and a way to rewrite any file), the exact setup of one
# game (``overrides``), environment variables that load code or name files.
_ENV_NAME = re.compile(r"^(PROTON_[A-Z0-9_]{1,40}|DXVK_[A-Z0-9_]{1,40}|VKD3D_[A-Z0-9_]{1,40}|RADV_[A-Z0-9_]{1,40}|"
                       r"WINE_CPU_TOPOLOGY|WINE_FULLSCREEN_FSR(_[A-Z]+)?|WINE_LARGE_ADDRESS_AWARE|WINEESYNC|WINEFSYNC|"
                       r"PULSE_LATENCY_MSEC|LC_ALL|LANG|DRI_PRIME|__NV_PRIME_RENDER_OFFLOAD|__GLX_VENDOR_LIBRARY_NAME|"
                       r"__VK_LAYER_NV_optimus|SDL_[A-Z0-9_]{1,40})$")
_ENV_NEVER = re.compile(r"PATH|PRELOAD|ICD|LOADER|DRIVER_FILES|LIBRARY_PATH|FILE|_DIR$|_LOG|HUD_CONFIG|"
                        r"CMD|_BIN$|EXEC|DYNAMIC_API|REMOTE_DEBUG")    # (PROTON_REMOTE_DEBUG_CMD starts a program)
_ENV_VALUE = re.compile(r"^[A-Za-z0-9 _.,:;=+\-()\[\]#@]{0,160}$")      # (and quoted by with_env)
_GAME_ARGS = re.compile(r"^[A-Za-z0-9 _.,:=+\-/]{0,200}$")    # put into the /bin/sh line unquoted: no shell characters
_WORD = re.compile(r"^[a-z0-9_]{1,40}$")
_DLL = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_DLL_MODES = {"", "n", "b", "n,b", "b,n", "d"}
_COMPAT_SET = {"use_runtime", "protonfixes", "report_app_id"}
_SIG_KEYS = {"engine", "code", "module", "null", "build", "replaced", "loaders", "box", "missing", "keys", "system"}


def _txt(v, n: int = 160) -> str:
    """Plain text of bounded length (no markup, no control characters)."""
    return "".join(ch for ch in str(v if v is not None else "") if ch.isprintable() and ch not in "<>")[:n]


def _clean_fix(fx) -> dict | None:
    """A shared fix rebuilt from what is allowed — or None (the whole fix is dropped, never half-applied)."""
    from ezgd.platform.proton import OPTIONS_BY_KEY
    if not isinstance(fx, dict):
        return None
    if fx.get("raw_options") or fx.get("edits"):
        return None
    gen_id = str(fx.get("gen_id") or "")
    if not re.fullmatch(r"[\w:.\-]{1,80}", gen_id):
        return None
    env = fx.get("env") or {}
    dlls = fx.get("dll_overrides") or {}
    lists = {k: fx.get(k) or [] for k in ("options", "fixes_on", "fixes_off", "winetricks")}
    compat = fx.get("compat_set") or {}
    args = fx.get("args") or ""
    if not (isinstance(env, dict) and isinstance(dlls, dict) and isinstance(compat, dict) and isinstance(args, str)
            and all(isinstance(v, list) for v in lists.values())):
        return None
    if len(env) > 8 or any(not (isinstance(k, str) and isinstance(v, str) and _ENV_NAME.match(k)
                                and not _ENV_NEVER.search(k) and _ENV_VALUE.match(v)) for k, v in env.items()):
        return None
    if len(dlls) > 16 or any(not (isinstance(k, str) and _DLL.match(k) and v in _DLL_MODES) for k, v in dlls.items()):
        return None
    if not _GAME_ARGS.match(args):
        return None
    if any(not isinstance(o, str) or o not in OPTIONS_BY_KEY for o in lists["options"]):
        return None
    for k in ("fixes_on", "fixes_off", "winetricks"):
        if len(lists[k]) > 8 or any(not (isinstance(x, str) and _WORD.match(x)) for x in lists[k]):
            return None
    if any(k not in _COMPAT_SET or not isinstance(v, bool) for k, v in compat.items()):
        return None
    return {"gen_id": gen_id, "label": _txt(fx.get("label"), 120), "why": _txt(fx.get("why"), 400), "env": env,
            "args": args, "dll_overrides": dlls, "options": lists["options"], "fixes_on": lists["fixes_on"],
            "fixes_off": lists["fixes_off"], "compat_set": compat, "winetricks": lists["winetricks"]}


def _clean_machine(m) -> dict:
    if not isinstance(m, dict):
        return {}
    cpu = m.get("cpu") if isinstance(m.get("cpu"), dict) else {}
    gpus = [g for g in (m.get("gpus") or []) if isinstance(g, dict)][:4] if isinstance(m.get("gpus"), list) else []
    num = lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e12 else 0  # noqa: E731
    mid = str(m.get("id") or "")
    return {"id": mid if re.fullmatch(r"[0-9a-f]{1,32}", mid) else "",
            **{k: _txt(m.get(k), 80) for k in ("os", "distro", "kernel", "desktop", "session", "audio")},
            "cpu": {"model": _txt(cpu.get("model"), 80), "vendor": _txt(cpu.get("vendor"), 16), "cores": num(cpu.get("cores")),
                    "flags": [f for f in (cpu.get("flags") or []) if isinstance(f, str) and _WORD.match(f)][:20]
                    if isinstance(cpu.get("flags"), list) else []},
            "gpus": [{"name": _txt(g.get("name"), 80), "driver": _txt(g.get("driver"), 60),
                      "vendor": _txt(g.get("vendor"), 16)} for g in gpus],
            "ram_gb": num(m.get("ram_gb")), "max_map_count": num(m.get("max_map_count"))}


def clean_record(rec) -> dict | None:
    """A solved failure from another computer, rebuilt from what may be shared (None: nothing usable)."""
    if not isinstance(rec, dict) or not isinstance(rec.get("sig"), dict):
        return None
    sig = {}
    for k, v in rec["sig"].items():
        if k not in _SIG_KEYS:
            continue
        if isinstance(v, bool) or (isinstance(v, int) and abs(v) < 2 ** 40):
            sig[k] = v
        elif isinstance(v, str):
            sig[k] = _txt(v, 120)
        elif isinstance(v, list):
            sig[k] = [_txt(x, 80) for x in v[:20] if isinstance(x, str)]
    fixes = [f for f in (_clean_fix(x) for x in (rec.get("fixes") or [])[:8]) if f is not None] \
        if isinstance(rec.get("fixes"), list) else []
    labels = [_txt(x, 160) for x in (rec.get("labels") or [])[:5] if isinstance(x, str)] \
        if isinstance(rec.get("labels"), list) else []
    at = rec.get("at")
    out = {"sig": sig, "game": _txt(rec.get("game"), 120), "fixes": fixes, "labels": labels,
           "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0,
           "machine": _clean_machine(rec.get("machine"))}
    n = rec.get("n")
    if isinstance(n, int) and not isinstance(n, bool) and 0 < n < 10 ** 7:
        out["n"] = n                                   # (everyone's knowledge: on how many networks it worked)
    return out


def shareable(rec: dict) -> dict | None:
    """What this computer shares of its own record: the same rules (no exact setup, no file edits, no raw lines)."""
    return clean_record(rec)


def shared_records(records: list) -> list[dict]:
    """What other computers solved, cleaned (records as read from their decrypted files)."""
    out = []
    for r in records[:2000]:
        c = clean_record(r)
        if c is not None:
            out.append(c)
    return out


def recall(path: Path, t: Trace, engine: str | None, game: str, shared: list[dict] | None = None,
           here: dict | None = None) -> list[Suspect]:
    """Failures solved before that look like this one — on this computer, or (``shared``) on others through the
    synced folder: their fixes as suspects, strongest first; a match from a different kind of computer counts for
    less, and one that hinges on another GPU vendor's driver not at all."""
    import time as _time
    from ezgd.library.fixgen import GeneratedFix
    from ezgd.platform import machine as machine_mod
    here = here if here is not None else _this_machine()
    sig = fingerprint(t, engine)
    out: list[Suspect] = []
    fields = set(GeneratedFix.__dataclass_fields__)
    own = _load_memory(path)["solved"]
    for mine, rec in [(True, r) for r in reversed(own)] + \
            [(False, r) for r in reversed(shared_records(shared or []))]:
        conf, why = _similar(sig, rec.get("sig") or {})
        if conf <= 0:
            continue
        there = rec.get("machine") or {}
        alike, diff = machine_mod.similarity(here, there) if there and here else (1.0, [])
        mod = (rec.get("sig") or {}).get("module") or ""
        vendor_there, vendor_here = machine_mod.gpu_vendor(there), machine_mod.gpu_vendor(here)
        if vendor_there and vendor_here and vendor_there != vendor_here and \
                any(rx.match(mod) for rx in _VENDOR_MODULES.values()):
            continue                          # a driver-specific crash on another vendor's card: not this problem
        conf = round(conf * (0.6 + 0.4 * alike), 3)
        when = _time.strftime("%Y-%m-%d", _time.localtime(rec.get("at") or 0))
        if "n" in rec:                                 # everyone's knowledge (crowd.py): more places, more sure
            conf = round(conf * min(1.0, 0.65 + 0.1 * rec["n"]), 3)
            where = (f"games on {rec['n']} other computers" if rec["n"] > 1 else "a game on another computer") + \
                (f" ({'; '.join(diff)})" if diff else "")
        else:
            where = "this game" if rec.get("game") == game else rec.get("game", "another game")
            if there.get("id") and here.get("id") and there["id"] != here["id"]:
                where += " on another computer" + (f" ({'; '.join(diff)})" if diff else "")
        label = "; ".join(rec.get("labels") or []) or "the fix that worked"
        text = f"This looks like the problem solved in {where} on {when} — {why}. What fixed it: {label}."
        if mine and rec.get("game") == game and rec.get("overrides") and \
                not (there.get("id") and here.get("id") and there["id"] != here["id"]):
            out.append(Suspect(f"memory:{_sig_id(rec['sig'])}:{game}", f"Again: {label}", text, conf,
                               overrides=rec["overrides"]))
            continue
        for fx in rec.get("fixes") or []:
            try:
                g = GeneratedFix(**{k: v for k, v in fx.items() if k in fields})
            except TypeError:
                continue
            if not (g.dll_overrides or g.env or g.args or g.options or g.fixes_on or g.fixes_off or g.winetricks
                    or g.compat_set or g.raw_options):
                continue
            g.learned_from = rec.get("game", "")
            out.append(Suspect(f"memory:{g.gen_id}", g.label, text, conf, g))
    best: dict[str, Suspect] = {}
    for s in out:
        if s.key not in best or s.confidence > best[s.key].confidence:
            best[s.key] = s
    return sorted(best.values(), key=lambda s: -s.confidence)


# ------------------------------------------------------------------------- the run that worked
NOTABLE = ("onlinefix64.dll", "onlinefix.dll", "ue4ss.dll", "steam_api64.dll", "steam_api.dll", "steamclient64.dll",
           "steamclient.dll", "d3d12.dll", "d3d12core.dll", "d3d11.dll", "dxgi.dll", "nvapi64.dll", "xinput1_3.dll",
           "eossdk-win64-shipping.dll", "galaxy64.dll")


def summary(t: Trace) -> dict:
    """What a run that worked looked like, kept to compare later failures against."""
    import time as _time
    from ezgd.platform.proton import game_binary_dirs
    dirs = game_binary_dirs(Path(t.exe))
    game = [ld for ld in t.loads if not t.game_tids or ld.tid in t.game_tids]
    own = {}
    for ld in game:
        if ld.name.endswith(".dll") and _in(_win_dir(ld.path), dirs) is not None:
            own[ld.name] = ld.kind
    missing = sorted({p for _t, p, _a in t.files_missing if not _OPTIONAL.search(p)})[:300]
    return {"at": _time.time(), "proton": t.setup.get("proton"), "proton_build": t.setup.get("proton_build"),
            "own": own, "notable": sorted({ld.name for ld in game if ld.name in NOTABLE}), "phase": t.phase,
            "missing": missing, "game_errors": [list(x) for x in t.game_errors]}   # (harmless: it worked)


def compare(good: dict, t: Trace) -> tuple[list[str], list[Suspect]]:
    """A failing run against the last one that worked: what is different inside the run (not just the setup)."""
    import time as _time
    from ezgd.library.fixgen import GeneratedFix
    when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(good.get("at") or 0))
    now = summary(t)
    lines: list[str] = []
    out: list[Suspect] = []
    switched = [n for n, k in (good.get("own") or {}).items() if k == "native" and now["own"].get(n) == "builtin"]
    if switched:
        lines.append(f"In the run that worked ({when}) the game loaded its own " + ", ".join(switched)
                     + "; now Wine's built-in " + ("copy is" if len(switched) == 1 else "copies are") + " used.")
        g = GeneratedFix("trace:game-dlls", "Load the game's own " + ", ".join(switched),
                         "It worked with the game's own copies.", dll_overrides={n[:-4]: "n,b" for n in switched})
        out.append(Suspect("game-dlls:" + ",".join(switched), g.label, "That is how it ran when it worked.", 0.9, g,
                           {"native": switched}))
    gone = [n for n in good.get("notable") or [] if n not in now["notable"]]
    if gone:
        lines.append(f"These loaded in the run that worked and not now: {', '.join(gone)}.")
    if good.get("proton_build") and now.get("proton_build") and good["proton_build"] != now["proton_build"]:
        lines.append(f"It worked with {good['proton_build']}; this run used {now['proton_build']}.")
    if not lines and len(now["phase"]) < len(good.get("phase") or []):
        lines.append(f"The run that worked ({when}) got further: " + " → ".join(good.get("phase") or []) + ".")
    return lines, out
