"""Steam and Proton integration (detection, non-Steam shortcuts).

Nothing here changes Steam settings silently: adding a shortcut writes
``shortcuts.vdf`` only after the user asks, keeps a timestamped backup, and
warns that Steam must be closed (Steam rewrites the file when it exits).
Selecting a Proton version for the shortcut is left to Steam's own
"Force the use of a specific Steam Play compatibility tool" option.
"""

from __future__ import annotations

import binascii
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ezgd.platform import IS_LINUX, IS_WINDOWS
from ezgd.platform import vdf

log = logging.getLogger(__name__)


@dataclass
class ProtonInstall:
    name: str
    path: Path            # directory containing the ``proton`` script

    @property
    def script(self) -> Path:
        return self.path / "proton"

    @property
    def manifest(self) -> dict:
        return read_tool_manifest(self.path)

    @property
    def require_tool_appid(self) -> int | None:
        v = self.manifest.get("require_tool_appid")
        return int(v) if isinstance(v, str) and v.isdigit() else None

    @property
    def has_protonfixes(self) -> bool:
        """GE-Proton / Proton-CachyOS bundle umu-protonfixes; Valve's builds do not."""
        return (self.path / "protonfixes").is_dir()

    def has_fix_for(self, app_id: int | None) -> bool:
        return bool(app_id) and (self.path / "protonfixes" / "gamefixes-steam" / f"{app_id}.py").is_file()

    @property
    def wine_bin(self) -> Path | None:
        for sub in ("files", "dist"):
            p = self.path / sub / "bin" / "wine"
            if p.is_file():
                return p
        return None


@dataclass
class SteamRuntime:
    app_id: int
    name: str
    path: Path            # e.g. steamapps/common/SteamLinuxRuntime_4

    def command(self, verb: str = "waitforexitandrun") -> list[str]:
        """The runtime's own commandline from its toolmanifest, e.g. _v2-entry-point --verb=… --"""
        line = read_tool_manifest(self.path).get("commandline", "/_v2-entry-point --verb=%verb% --")
        parts = line.replace("%verb%", verb).split()
        return [str(self.path) + parts[0]] + parts[1:]


def read_tool_manifest(tool_dir: Path) -> dict:
    p = tool_dir / "toolmanifest.vdf"
    if not p.is_file():
        return {}
    try:
        data = vdf.text_loads(p.read_text("utf-8", "replace"))
    except (vdf.VdfError, OSError):
        return {}
    m = data.get("manifest")
    return m if isinstance(m, dict) else {}


def find_runtime(steam_root: Path | None, app_id: int | None) -> SteamRuntime | None:
    """Locate the Steam Linux Runtime a compatibility tool requires (``require_tool_appid``),
    using Steam's own ``appmanifest_<id>.acf`` files — the same lookup Steam performs."""
    if not steam_root or not app_id:
        return None
    for lib in library_folders(steam_root):
        acf = lib / "steamapps" / f"appmanifest_{app_id}.acf"
        if not acf.is_file():
            continue
        try:
            state = vdf.text_loads(acf.read_text("utf-8", "replace")).get("AppState", {})
        except (vdf.VdfError, OSError):
            continue
        installdir = state.get("installdir")
        if not installdir:
            continue
        path = lib / "steamapps" / "common" / installdir
        entry = read_tool_manifest(path).get("commandline", "").split()
        if entry and (path / entry[0].lstrip("/")).is_file():
            return SteamRuntime(app_id, state.get("name") or installdir, path)
    return None


def steam_root_candidates() -> list[Path]:
    home = Path.home()
    if IS_WINDOWS:
        out = []
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                out.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except OSError:
            pass
        out += [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
        return out
    return [home / ".steam" / "steam", home / ".steam" / "root", home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
            home / "snap" / "steam" / "common" / ".local" / "share" / "Steam"]


def find_steam_root(explicit: str | None = None) -> Path | None:
    """The Steam folder: the one set in Settings (only that one — a wrong setting is not "corrected" to some
    other Steam), else ``EZGD_STEAM_ROOT``, else the usual places."""
    explicit = explicit or os.environ.get("EZGD_STEAM_ROOT") or None
    cands = [Path(explicit).expanduser()] if explicit else steam_root_candidates()
    for c in cands:
        try:
            if (c / "steamapps").is_dir() or (c / "userdata").is_dir():
                return c.resolve()
        except OSError:
            continue
    return None


def library_folders(steam_root: Path) -> list[Path]:
    out = [steam_root]
    lf = steam_root / "steamapps" / "libraryfolders.vdf"
    if lf.is_file():
        try:
            data = vdf.text_loads(lf.read_text("utf-8", "replace"))
            for v in (data.get("libraryfolders") or data.get("LibraryFolders") or {}).values():
                if isinstance(v, dict):          # current format: "0" { "path" "..." }
                    p = v.get("path")
                elif isinstance(v, str):         # legacy format: "1" "D:\\SteamLibrary"
                    p = v
                else:
                    p = None
                if p:
                    pp = Path(p)
                    if pp not in out and pp.is_dir():
                        out.append(pp)
        except (vdf.VdfError, OSError) as exc:
            log.warning("could not read libraryfolders.vdf: %s", exc)
    return out


def find_proton_versions(steam_root: Path | None) -> list[ProtonInstall]:
    """Official Proton builds in every Steam library plus custom tools (e.g. GE-Proton)."""
    if not IS_LINUX:
        return []
    found: dict[str, ProtonInstall] = {}
    roots = library_folders(steam_root) if steam_root else []
    for lib in roots:
        common = lib / "steamapps" / "common"
        if common.is_dir():
            for d in common.iterdir():
                if d.name.lower().startswith("proton") and (d / "proton").is_file():
                    found.setdefault(d.name, ProtonInstall(d.name, d))
    custom_dirs = [Path.home() / ".steam" / "root" / "compatibilitytools.d"]
    if steam_root:
        custom_dirs.insert(0, steam_root / "compatibilitytools.d")
    for cd in custom_dirs:
        if cd.is_dir():
            for d in cd.iterdir():
                if (d / "proton").is_file():
                    found.setdefault(d.name, ProtonInstall(d.name, d))
    return sorted(found.values(), key=_proton_sort_key)


def _proton_sort_key(p: ProtonInstall):
    """Default preference: custom builds (e.g. GE-Proton, usually best for non-Steam games),
    then Proton Experimental, then official releases — newest first within each group."""
    nums = [-int(x) for x in re.findall(r"\d+", p.name)]
    if p.path.parent.name == "compatibilitytools.d":
        group = 0
    elif "experimental" in p.name.lower():
        group = 1
    else:
        group = 2
    return (group, nums, p.name)


def steam_users(steam_root: Path) -> list[str]:
    ud = steam_root / "userdata"
    if not ud.is_dir():
        return []
    return sorted(d.name for d in ud.iterdir() if d.is_dir() and d.name.isdigit() and d.name != "0")


def shortcut_appid(exe: str, name: str) -> int:
    """Steam's id for non-Steam shortcuts: crc32(exe + name) | 0x80000000 (unsigned)."""
    return (binascii.crc32((exe + name).encode("utf-8")) & 0xFFFFFFFF) | 0x80000000


def shortcut_rungameid(appid: int) -> int:
    return (appid << 32) | 0x02000000


def is_steam_running() -> bool:
    try:
        if IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"], capture_output=True, text=True,
                                 timeout=5, creationflags=subprocess.CREATE_NO_WINDOW).stdout
            return "steam.exe" in out.lower()
        if shutil.which("pgrep"):
            return subprocess.run(["pgrep", "-x", "steam"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return False


RUNNER_MARK = "--play "          # launch options of shortcuts that start a game through this app


def shortcuts_of(steam_root: Path, user_id: str) -> list[dict]:
    path = steam_root / "userdata" / user_id / "config" / "shortcuts.vdf"
    try:
        raw = path.read_bytes()
        data = vdf.binary_loads(raw) if raw else {}
    except Exception:                         # missing or unreadable: no shortcuts
        return []
    return [v for v in (data.get("shortcuts") or {}).values() if isinstance(v, dict)]


def has_shortcut(steam_root: Path, appid: int, exe: Path, launch_options: str) -> bool:
    """True if every Steam user on this computer has this exact shortcut (what Steam knows after its last start)."""
    signed = appid - 2**32 if appid >= 2**31 else appid
    users = steam_users(steam_root)
    if not users:
        return False
    for user in users:
        if not any(int(v.get("appid", 0)) in (appid, signed) and v.get("Exe") == f'"{exe}"'
                   and v.get("LaunchOptions") == launch_options for v in shortcuts_of(steam_root, user)):
            return False
    return True


def steam_command() -> list[str] | None:
    """How to talk to the Steam client (``steam steam://…``, ``steam -shutdown``)."""
    exe = shutil.which("steam")
    if exe:
        return [exe]
    if shutil.which("flatpak") and subprocess.run(["flatpak", "info", "com.valvesoftware.Steam"],
                                                  capture_output=True).returncode == 0:
        return ["flatpak", "run", "com.valvesoftware.Steam"]
    if IS_WINDOWS:
        for root in steam_root_candidates():
            if (root / "steam.exe").is_file():
                return [str(root / "steam.exe")]
    return None


def shutdown_steam(timeout: float = 40.0) -> bool:
    """Ask the Steam client to exit (it closes itself normally) and wait; True once it is gone."""
    cmd = steam_command()
    if cmd is None:
        return not is_steam_running()
    if is_steam_running():
        try:
            subprocess.Popen(cmd + ["-shutdown"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_steam_running():
            time.sleep(1.0)                     # let it finish writing its files
            return True
        time.sleep(0.5)
    return False


def open_steam_url(url: str) -> None:
    """Hand a steam:// link to the Steam client (starting Steam if it is not running)."""
    cmd = steam_command()
    if cmd is None:
        raise RuntimeError("Steam was not found.")
    # -silent: if Steam has to start for this, it starts in the background — no Steam window over the game
    subprocess.Popen(cmd + ["-silent", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def overlay_env(steam_root: Path | None, inherited: str = "") -> dict[str, str] | None:
    """Environment that loads the Steam overlay into a game Steam did not start (Linux, Steam running).

    The same two pieces Steam sets for its own launches: the overlay renderer preloaded into the game's processes
    (OpenGL/X11; Steam Linux Runtime / pressure-vessel carries it into its container) and Steam's Vulkan overlay
    layer, which DXVK/VKD3D games under Proton draw through. Checked 2026-10-11: Steam attaches its overlay
    ("GameOverlay: started … for game process …" in console_log.txt) with no shortcut, no restart and no app id."""
    if IS_WINDOWS or steam_root is None:
        return None
    libs = [steam_root / "ubuntu12_32" / "gameoverlayrenderer.so", steam_root / "ubuntu12_64" / "gameoverlayrenderer.so"]
    if not all(p.is_file() for p in libs):
        return None
    keep = [x for x in re.split(r"[:\s]+", inherited or "") if x and "gameoverlayrenderer" not in x]
    return {"LD_PRELOAD": ":".join(keep + [str(p) for p in libs]), "ENABLE_VK_LAYER_VALVE_steam_overlay_1": "1"}


def remove_shortcut(steam_root: Path, appid: int) -> list[str]:
    """Remove the non-Steam shortcut with ``appid`` from every Steam user (backup kept); returns the users changed."""
    changed = []
    signed = appid - 2**32 if appid >= 2**31 else appid
    for user in steam_users(steam_root):
        path = steam_root / "userdata" / user / "config" / "shortcuts.vdf"
        if not path.exists():
            continue

        def change(shortcuts: dict) -> bool:
            gone = [k for k, v in shortcuts.items() if isinstance(v, dict) and int(v.get("appid", 0)) in (appid, signed)]
            for k in gone:
                del shortcuts[k]
            return bool(gone)
        try:
            if write_shortcuts(path, change, ours=lambda v: _is_app_shortcut(v) or
                               int(v.get("appid", 0)) in (appid, signed)) is not None:
                changed.append(user)
        except SteamFileError:
            continue
    return changed


class SteamFileError(RuntimeError):
    """A change to Steam's files was refused because it could not be verified (nothing was written)."""


def _native_entry(appid: int, name: str, exe: Path, start_dir: Path, launch_options: str) -> dict:
    """A shortcut exactly as Steam writes one itself (checked against Steam-written shortcuts.vdf, 2026-10-12):
    same fields in the same order, unquoted start folder with a trailing slash, no icon path, no tags."""
    start = str(start_dir).rstrip("/\\") + ("\\" if IS_WINDOWS else "/")
    return {
        "appid": vdf.Int32(appid - 2**32 if appid >= 2**31 else appid),
        "AppName": name, "Exe": f'"{exe}"', "StartDir": start, "icon": "", "ShortcutPath": "",
        "LaunchOptions": launch_options, "IsHidden": vdf.Int32(0), "AllowDesktopConfig": vdf.Int32(1),
        "AllowOverlay": vdf.Int32(1), "OpenVR": vdf.Int32(0), "Devkit": vdf.Int32(0), "DevkitGameID": "",
        "DevkitOverrideAppID": vdf.Int32(0), "LastPlayTime": vdf.Int32(0), "FlatpakAppID": "", "sortas": "",
        "tags": {},
    }


def _is_app_shortcut(v: dict) -> bool:
    return RUNNER_MARK in str(v.get("LaunchOptions", ""))


class SteamDuplicateError(SteamFileError):
    """The game is in Steam already (nothing was written): ``existing`` is the name Steam shows it under."""

    def __init__(self, message: str, existing: str, by_app: bool):
        super().__init__(message)
        self.existing = existing
        self.by_app = by_app


def play_id(v: dict) -> int | None:
    """The Library id an app shortcut starts (``… --play 18``), else None."""
    m = re.search(r"--play\s+'?(\d+)", str(v.get("LaunchOptions", "")))
    return int(m.group(1)) if m and _is_app_shortcut(v) else None


def _norm_name(name: str) -> str:
    """``"PEAK.exe"`` / ``PEAK™`` / ``peak`` → ``peak``: how a shortcut's name is compared with a game's title."""
    n = str(name or "").strip().strip('"')
    n = re.split(r"[\\/]", n)[-1]
    n = re.sub(r"\.(exe|bat|cmd|sh|x86_64|x86|appimage|lnk)$", "", n, flags=re.I)
    return re.sub(r"[\W_]+", "", n.casefold())


def _exe_path(v: dict) -> Path | None:
    raw = str(v.get("Exe", "")).strip().strip('"')
    return Path(raw) if raw else None


def same_game_shortcut(shortcuts: list[dict], *, title: str, game_exe: str | None = None,
                       game_dir: str | None = None, entry_id: int | None = None,
                       live_ids: set[int] | None = None) -> dict | None:
    """A shortcut in this list that already is this game, other than the app's own for ``entry_id``:

    * one made outside the app (by the user, Steam or another tool) that starts the game's executable or anything
      in its folder, or has the game's name (``PEAK.exe`` for *PEAK*);
    * the app's shortcut for *another* Library entry of the same game that still exists (``live_ids``).
    Returns ``{"name", "by_app"}`` or None."""
    key = _norm_name(title)
    exe = Path(game_exe).resolve() if game_exe else None
    folder = Path(game_dir).resolve() if game_dir else None
    if folder is not None and len(folder.parts) < 4:      # never "anything under /home/me"
        folder = None
    for v in shortcuts:
        pid = play_id(v)
        if pid is not None:
            if pid != entry_id and live_ids is not None and pid in live_ids and len(key) >= 2 \
                    and _norm_name(v.get("AppName", "")) == key:
                return {"name": str(v.get("AppName", "")), "by_app": True}
            continue
        if _is_app_shortcut(v):
            continue
        p = _exe_path(v)
        try:
            rp = p.resolve() if p is not None else None
        except (OSError, RuntimeError):
            rp = p
        same = (exe is not None and rp == exe) or (folder is not None and rp is not None and folder in rp.parents)
        if same or (len(key) >= 3 and _norm_name(v.get("AppName", "")) == key):
            return {"name": str(v.get("AppName", "")), "by_app": False}
    return None


def steam_copy_of(steam_root: Path, **kw) -> dict | None:
    """``same_game_shortcut`` over every Steam user's list (what Steam shows after its next start)."""
    for user in steam_users(steam_root):
        hit = same_game_shortcut(shortcuts_of(steam_root, user), **kw)
        if hit is not None:
            return hit
    return None


def write_shortcuts(path: Path, change, ours=_is_app_shortcut) -> Path | None:
    """Apply ``change(shortcuts: dict) -> bool`` to a shortcuts.vdf, safely; returns the backup made (or None).

    Never while Steam is running (Steam rewrites the file when it exits). The new file is only written if it reads
    back identically and every shortcut that was not the app's is byte-for-byte unchanged; a backup is kept."""
    if is_steam_running():
        raise SteamFileError("Steam is running — its files are only changed while it is closed.")
    raw = path.read_bytes() if path.exists() else b""
    try:
        data = vdf.binary_loads(raw) if raw else {"shortcuts": {}}
    except Exception as exc:
        raise SteamFileError(f"Steam's shortcut list could not be read, so it was left alone ({exc}).") from exc
    shortcuts = data.setdefault("shortcuts", {})

    def others(sc: dict) -> list[bytes]:
        return sorted(vdf.binary_dumps({"x": v}) for v in sc.values() if isinstance(v, dict) and not ours(v))
    before = others(shortcuts)
    if not change(shortcuts):
        return None
    ordered = sorted(shortcuts.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 10**9)
    data["shortcuts"] = {str(i): v for i, (_k, v) in enumerate(ordered)}      # 0..n-1, like Steam
    for v in data["shortcuts"].values():
        if isinstance(v, dict) and _is_app_shortcut(v) and (v.get("icon") or v.get("tags")):
            # 2026-10-12: an .exe as "icon" made Steam's Library crash (memstd.cpp fatal assert) — never again
            raise SteamFileError("Refusing to write an app shortcut with an icon or tags (that can crash Steam).")
    out = vdf.binary_dumps(data)
    check = vdf.binary_loads(out)
    if vdf.binary_dumps(check) != out:
        raise SteamFileError("The new shortcut list did not verify, so nothing was written.")
    if others(check["shortcuts"]) != before:
        raise SteamFileError("Writing would have changed shortcuts that are not the app's, so nothing was written.")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if raw:
        backup = path.with_name(f"shortcuts.vdf.ezgd-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(raw)
    tmp = path.with_suffix(".ezgd-tmp")
    tmp.write_bytes(out)
    if is_steam_running():                     # Steam started meanwhile: do not touch its file
        tmp.unlink(missing_ok=True)
        raise SteamFileError("Steam started while the list was being written — nothing was changed.")
    os.replace(tmp, path)
    return backup


def add_shortcut(steam_root: Path, user_id: str, *, name: str, exe: Path, start_dir: Path,
                 launch_options: str = "", icon: str = "", game_exe: str | None = None, game_dir: str | None = None,
                 live_ids: set[int] | None = None) -> int:
    """Add (or update) a non-Steam game shortcut in Steam's own format; returns its app id. ``icon`` is ignored:
    Steam's own shortcuts have none on Linux, and an .exe path there is not an image.

    A game is never added twice: if Steam already has it (see :func:`same_game_shortcut` — ``game_exe`` /
    ``game_dir`` are the game's own files, ``live_ids`` the Library's entries) nothing is written and
    :class:`SteamDuplicateError` says under which name. The app's own shortcut for the same game is updated in
    place, also when the app has moved since (another ``Exe``), and extra copies of it are taken out."""
    path = steam_root / "userdata" / user_id / "config" / "shortcuts.vdf"
    exe_q = f'"{exe}"'
    appid = shortcut_appid(exe_q, name)
    entry = _native_entry(appid, name, exe, start_dir, launch_options)
    mine = play_id({"LaunchOptions": launch_options})
    hit = same_game_shortcut(shortcuts_of(steam_root, user_id), title=name, game_exe=game_exe, game_dir=game_dir,
                             entry_id=mine, live_ids=live_ids) if mine is not None else None
    if hit is not None:
        where = "for another copy of it in your Library" if hit["by_app"] else "(added outside this app)"
        raise SteamDuplicateError(f"Steam already has this game as “{hit['name']}” {where}, so it was not added "
                                  "a second time.", hit["name"], hit["by_app"])

    def same(v: dict) -> bool:
        if not isinstance(v, dict):
            return False
        if mine is not None:                   # the app's shortcut for this game, wherever the app was then
            if play_id(v) == mine:
                return True
            # a leftover of a Library entry that is gone, with this game's name: reused instead of a second one
            pid = play_id(v)
            return pid is not None and live_ids is not None and pid not in live_ids and \
                _norm_name(v.get("AppName", "")) == _norm_name(name)
        return v.get("Exe") == exe_q

    def change(shortcuts: dict) -> bool:
        keys = [k for k, v in shortcuts.items() if same(v)]
        if not keys:
            shortcuts[str(10**6 + len(shortcuts))] = entry   # appended; renumbered by write_shortcuts
            return True
        first, extra = keys[0], keys[1:]
        if not extra and vdf.binary_dumps({"x": shortcuts[first]}) == vdf.binary_dumps({"x": entry}):
            return False                       # already exactly there: nothing to write
        shortcuts[first] = entry
        for k in extra:                        # the same game more than once (older versions): only one stays
            del shortcuts[k]
        return True
    # the entry being added/updated is the one this call may touch; every other shortcut must stay as it is
    write_shortcuts(path, change, ours=lambda v: _is_app_shortcut(v) or same(v))
    return appid


def remove_app_shortcuts(steam_root: Path) -> int:
    """Remove every shortcut this app added (they start ``… --play <id>``) from all Steam users; returns how many."""
    removed = 0
    for user in steam_users(steam_root):
        path = steam_root / "userdata" / user / "config" / "shortcuts.vdf"
        if not path.exists():
            continue
        count = [0]

        def change(shortcuts: dict, count=count) -> bool:
            for k in [k for k, v in shortcuts.items() if isinstance(v, dict)
                      and RUNNER_MARK in str(v.get("LaunchOptions", ""))]:
                del shortcuts[k]
                count[0] += 1
            return bool(count[0])
        write_shortcuts(path, change)
        removed += count[0]
    return removed


def restore_shortcut_backup(backup: Path) -> None:
    """Put a shortcuts.vdf backup back (Steam closed)."""
    if is_steam_running():
        raise SteamFileError("Steam is running — close it first.")
    target = backup.with_name("shortcuts.vdf")
    tmp = target.with_suffix(".ezgd-tmp")
    tmp.write_bytes(backup.read_bytes())
    os.replace(tmp, target)


def risky_app_shortcuts(steam_root: Path) -> int:
    """Shortcuts written by older versions of this app with an icon path or tags (an .exe as icon crashes Steam's
    Library: memstd.cpp "Fatal assert", 2026-10-12)."""
    n = 0
    for user in steam_users(steam_root):
        n += sum(1 for v in shortcuts_of(steam_root, user) if _is_app_shortcut(v) and (v.get("icon") or v.get("tags")))
    return n


def repair_risky_app_shortcuts(steam_root: Path) -> int:
    """Remove those entries (only the app's own, only while Steam is closed); returns how many were removed."""
    removed = 0
    for user in steam_users(steam_root):
        path = steam_root / "userdata" / user / "config" / "shortcuts.vdf"
        if not path.exists():
            continue
        count = [0]

        def change(shortcuts: dict, count=count) -> bool:
            for k in [k for k, v in shortcuts.items() if isinstance(v, dict) and _is_app_shortcut(v)
                      and (v.get("icon") or v.get("tags"))]:
                del shortcuts[k]
                count[0] += 1
            return bool(count[0])
        write_shortcuts(path, change)
        removed += count[0]
    return removed


def installed_games(steam_root: Path | None) -> list[dict]:
    """Games installed in Steam (read only): appid, name, folder — from every library's appmanifest_*.acf."""
    out: list[dict] = []
    if steam_root is None:
        return out
    for lib in library_folders(steam_root):
        apps = lib / "steamapps"
        for mf in sorted(apps.glob("appmanifest_*.acf")) if apps.is_dir() else []:
            try:
                st = (vdf.text_loads(mf.read_text("utf-8", "replace")).get("AppState") or {})
            except (vdf.VdfError, OSError):
                continue
            folder = apps / "common" / str(st.get("installdir") or "")
            flags = int(st.get("StateFlags") or 0) if str(st.get("StateFlags") or "0").isdigit() else 0
            if st.get("installdir") and folder.is_dir():
                out.append({"appid": int(st.get("appid") or 0), "name": str(st.get("name") or ""),
                            "folder": folder, "complete": bool(flags & 4) and not flags & 2})
    return out


def find_installed_game(steam_root: Path | None, name: str) -> dict | None:
    """The installed Steam game called ``name`` (e.g. "Little Nightmares III - Free Trial & Friend's Pass")."""
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("&", " and ")).strip()
    want = norm(name)
    if not want:
        return None
    games = installed_games(steam_root)
    exact = [g for g in games if norm(g["name"]) == want]
    if exact:
        return exact[0]
    # "… - Free Trial & Friend's Pass" is a free licence for the main game, installed under the main game's name:
    # the longest installed name contained in the wanted one ("Little Nightmares III", not "Little Nightmares")
    close = [g for g in games if want in norm(g["name"]) or (len(norm(g["name"])) > 8 and
                                                               f" {norm(g['name'])} " in f" {want} ")]
    if not close:
        return None
    close.sort(key=lambda g: len(norm(g["name"])), reverse=True)
    if len(close) > 1 and len(norm(close[0]["name"])) == len(norm(close[1]["name"])):
        return None                                       # two equally good matches: do not guess
    return close[0]


def base_title(name: str) -> str:
    """The main game's title for "Little Nightmares III - Free Trial & Friend's Pass" and the like."""
    parts = re.split(r"\s+[-–—:]\s+", name)
    if len(parts) > 1 and re.search(r"(trial|friend|demo|pass|prologue|playtest)", parts[-1], re.I):
        return " - ".join(parts[:-1]).strip()
    return name.strip()
