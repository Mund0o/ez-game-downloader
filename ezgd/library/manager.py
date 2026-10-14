"""Library operations: registration, executables, launching, duplicates, removal."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ezgd.core.fsutil import is_within
from ezgd.database.repositories import GameRepository, LibraryRepository
from ezgd.library import advisor, fixes, fixgen, repack, saves, steamrun
from ezgd.library.executables import Candidate, find_executables, rank
from ezgd.library.models import CompatConfig, LaunchMethod, LibraryEntry, LibraryStatus
from ezgd.platform import IS_LINUX
from ezgd.platform import launcher, steam
from ezgd.platform.paths import AppPaths
from ezgd.platform.proton import (OPTIONS_BY_KEY, prefix_ready, suggest_dll_overrides, validate_overrides,
                                  winetricks_env)

log = logging.getLogger(__name__)


class LibraryError(Exception):
    pass


def _prefix_processes(prefix: Path) -> list[int]:
    """PIDs of processes (wineserver, Wine programs) working on ``prefix`` — from their environment (Linux)."""
    marks = (f"STEAM_COMPAT_DATA_PATH={prefix}".encode(), f"WINEPREFIX={prefix}/pfx".encode(),
             f"WINEPREFIX={prefix}".encode())
    pids = []
    for d in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            env = (d / "environ").read_bytes()
        except OSError:
            continue
        if any(m + b"\0" in env or env.endswith(m) for m in marks):
            pids.append(int(d.name))
    return pids


def _stop_prefix_processes(prefix: Path, wait: float = 10.0) -> None:
    """Let a throw-away prefix's wineserver finish (it exits a few seconds after the game), then stop leftovers."""
    import signal
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and _prefix_processes(prefix):
        time.sleep(0.5)
    for pid in _prefix_processes(prefix):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.5)


def _split_options(opts: str) -> tuple[list[str], list[str]]:
    """Steam launch options → (tokens before %command%, game arguments after it)."""
    import shlex as _shlex
    tokens = _shlex.split(opts or "")
    if "%command%" in tokens:
        i = tokens.index("%command%")
        return tokens[:i], tokens[i + 1:]
    return [], tokens


def merge_overrides(a: dict, b: dict) -> dict | None:
    """Two tested setups as one; None when they contradict each other."""
    import shlex as _shlex
    out: dict = {}
    pa, pb = a.get("proton_version"), b.get("proton_version")
    if pa and pb and pa != pb:
        return None
    if pa or pb:
        out["proton_version"] = pa or pb
    la, lb = a.get("launch_args"), b.get("launch_args")
    if la and lb:
        try:
            (bef_a, aft_a), (bef_b, aft_b) = _split_options(la), _split_options(lb)
        except ValueError:
            return None
        keys = {t.split("=", 1)[0] for t in bef_a if "=" in t}
        if any("=" in t and t.split("=", 1)[0] in keys and t not in bef_a for t in bef_b):
            return None                                  # same variable, different value
        before = bef_a + [t for t in bef_b if t not in bef_a]
        after = aft_a + [t for t in aft_b if t not in aft_a]
        out["launch_args"] = " ".join([_shlex.quote(t) if "=" not in t else
                                       t.split("=", 1)[0] + "=" + _shlex.quote(t.split("=", 1)[1]) for t in before]
                                      + ["%command%"] + [_shlex.quote(t) for t in after])
    elif la or lb:
        out["launch_args"] = la or lb
    ca, cb = a.get("compat"), b.get("compat")
    if ca and cb:
        c = dict(ca)
        c["options"] = sorted(set(ca.get("options") or []) | set(cb.get("options") or []))
        c["fixes"] = sorted(set(ca.get("fixes") or []) | set(cb.get("fixes") or []))
        dlls: dict[str, str] = {}
        for part in ";".join(x for x in (ca.get("dll_overrides"), cb.get("dll_overrides")) if x).split(";"):
            if "=" not in part:
                continue
            name, mode = part.split("=", 1)
            if name.lower() in dlls and dlls[name.lower()] != mode:
                return None
            dlls[name.lower()] = mode
        c["dll_overrides"] = ";".join(f"{k}={v}" for k, v in dlls.items())
        out["compat"] = c
    elif ca or cb:
        out["compat"] = ca or cb
    edits = (a.get("edits") or []) + (b.get("edits") or [])
    if edits:
        out["edits"] = edits
    wa, wb = a.get("winetricks") or [], b.get("winetricks") or []
    if any(v.startswith("win") and v[3:].isdigit() or v in ("winxp", "vista") for v in wa) and \
            any(v.startswith("win") and v[3:].isdigit() or v in ("winxp", "vista") for v in wb):
        return None                                        # two different Windows versions
    if wa or wb:
        out["winetricks"] = wa + [v for v in wb if v not in wa]
    return out


def _drew_frames(text: str) -> bool | None:
    """Did the game start presenting frames? (DXVK / vkd3d-proton log their swap chain.) None = cannot tell."""
    import re as _re
    if _re.search(r"(Presenter: Actual swap chain properties|DxvkPresenter|vkd3d.*[Ss]wapchain|"
                  r"Creating swap ?chain|swapchain.*created|D3D11SwapChain|DXGI: .*Swapchain)", text):
        return True
    if _re.search(r"(DXVK: v\d|vkd3d-proton)", text):
        return False                                # the layer started, but no swap chain was ever made
    return None                                     # OpenGL / native Vulkan / WineD3D: no marker to read


def _trial_reason(text: str) -> str:
    """Short reason for a failed trial run, from its Wine log."""
    import re as _re
    m = _re.search(r"Library ([\w.\-]+\.dll) \(which is needed by", text, _re.I)
    if m:
        return f"{m.group(1)} not found"
    m = _re.search(r"unimplemented function ([\w.\-]+\.dll\.\S+)", text, _re.I)
    if m:
        return f"unimplemented function {m.group(1)}"
    m = _re.search(r"Unhandled exception code ([0-9a-f]{8}).*addr 0x([0-9a-f]+)", text, _re.I)
    if m:
        return {"c0000005": "access violation", "c000001d": "illegal CPU instruction"}.get(m.group(1).lower(),
                                                                                           f"exception {m.group(1)}")
    m = _re.search(r"(EasyAntiCheat|BattlEye)", text)
    if m:
        return f"{m.group(1)} anti-cheat"
    return ""


_OF_INI_CACHE: dict[str, tuple[float, list]] = {}      # game folder → (when, OnlineFix.ini files found)


class LibraryManager:
    on_game_exit = None          # (entry_id, exit code, seconds) after every game run — the fix follow-up

    @property
    def trial_evidence(self) -> dict[int, dict[int, dict]]:
        """entry id → trial number → that test run's log text, exit code, seconds (latest session only)."""
        if "_trial_evidence" not in self.__dict__:
            self._trial_evidence = {}
        return self._trial_evidence

    def __init__(self, repo: LibraryRepository, games: GameRepository, settings, paths: AppPaths,
                 emit: Callable[[str, Any], None]):
        self.repo = repo
        self.games = games
        self.settings = settings
        self.paths = paths
        self.emit = emit
        self._protons: list[steam.ProtonInstall] | None = None
        self._processes: dict[int, Any] = {}
        self._installers: dict[int, dict] = {}       # entry id → running installer (repack setup.exe)
        self._steam_seen: dict[int, dict] = {}       # games running through Steam (play mode), last seen
        self._launch_info: dict[int, dict] = {}      # Proton used / overlay loaded, per running game

    # ----------------------------------------------------------- environment
    def steam_root(self) -> Path | None:
        return steam.find_steam_root(self.settings.settings.steam_root or None)

    def protons(self, refresh: bool = False) -> list[steam.ProtonInstall]:
        if self._protons is None or refresh:
            self._protons = steam.find_proton_versions(self.steam_root())
        return self._protons

    def environment(self) -> dict:
        root = self.steam_root()
        protons = []
        for p in self.protons():
            rt = steam.find_runtime(root, p.require_tool_appid)
            protons.append({"name": p.name, "protonfixes": p.has_protonfixes,
                            "runtime": rt.name if rt else None, "needs_runtime": bool(p.require_tool_appid)})
        return {"steam_root": str(root) if root else None,
                "steam_users": steam.steam_users(root) if root else [],
                "protons": [p.name for p in self.protons()], "proton_details": protons,
                "wine": bool(shutil.which("wine")), "winetricks": bool(shutil.which("winetricks")),
                "linux": IS_LINUX}

    def _proton_for(self, e: LibraryEntry) -> steam.ProtonInstall | None:
        name = e.proton_version or self.settings.settings.proton_version or None
        protons = self.protons()
        return next((p for p in protons if p.name == name), protons[0] if protons else None)

    def prefix_dir(self, entry_id: int) -> Path:
        return self.paths.prefixes / f"game-{entry_id}"

    # ---------------------------------------------------------------- CRUD
    def all(self) -> list[LibraryEntry]:
        entries = self.repo.all()
        for e in entries:
            if e.status is not LibraryStatus.EXTRACTING:
                path = e.install_path or e.download_path
                if path and not Path(path).exists() and e.status is not LibraryStatus.BROKEN:
                    e.status = LibraryStatus.BROKEN
                    self.repo.update(e)
                elif e.status is LibraryStatus.BROKEN and path and Path(path).exists():
                    e.status = LibraryStatus.INSTALLED if e.executable else LibraryStatus.EXTRACTED
                    self.repo.update(e)
        return entries

    def get(self, entry_id: int) -> LibraryEntry:
        e = self.repo.get(entry_id)
        if e is None:
            raise LibraryError("Library entry not found.")
        return e

    def import_folder(self, title: str, folder: Path, game_id: str | None = None) -> LibraryEntry:
        """Register a game the user already has on disk (no files are moved)."""
        folder = Path(folder)
        if not folder.exists():
            raise LibraryError(f"{folder} does not exist")
        game = self.games.get(game_id) if game_id else None
        e = LibraryEntry(title=title.strip() or folder.name, game_id=game_id, source=game.source if game else None,
                         cover_url=game.cover_url if game else None,
                         status=LibraryStatus.EXTRACTED if folder.is_dir() else LibraryStatus.DOWNLOADED)
        if folder.is_dir():
            e.install_path = str(folder)
        else:
            e.download_path = str(folder)
        e = self.repo.insert(e)
        self.emit("library_changed", e.id)
        return e

    def duplicates(self, entry: LibraryEntry | int) -> list[LibraryEntry]:
        if not isinstance(entry, LibraryEntry):
            entry = self.get(int(entry))
        return self.repo.duplicates_of(entry)

    def update(self, entry_id: int, **changes) -> LibraryEntry:
        e = self.get(entry_id)
        for k, v in changes.items():
            if k == "launch_method":
                v = LaunchMethod(v)
            elif k == "status":
                v = LibraryStatus(v)
            elif k == "launch_args":
                v = str(v or "").strip()
                try:
                    launcher.validate_launch_options(v)     # reject options that cannot run before saving
                except launcher.LaunchError as exc:
                    raise LibraryError(str(exc)) from exc
            elif k == "steam_app_id":
                v = int(v) if v not in (None, "", 0, "0") else None
                if v is not None and not (0 < v < 2**32):
                    raise LibraryError("Steam App IDs are positive numbers.")
            elif k == "compat":
                cfg = CompatConfig.from_dict(v)
                if cfg.dll_overrides:
                    try:
                        cfg.dll_overrides = validate_overrides(cfg.dll_overrides)
                    except ValueError as exc:
                        raise LibraryError(str(exc)) from exc
                cfg.options = [o for o in cfg.options if o in OPTIONS_BY_KEY]
                v = cfg.to_dict()
            elif k not in ("title", "proton_version", "notes", "install_path"):
                raise LibraryError(f"field {k} cannot be changed directly")
            setattr(e, k, v)
        self.repo.update(e)
        self.emit("library_changed", e.id)
        return e

    # ----------------------------------------------------------- executables
    def candidates(self, entry_id: int) -> list[Candidate]:
        e = self.get(entry_id)
        root = Path(e.install_path or e.download_path or "")
        if not root.exists():
            return []
        hint = None
        if e.game_id:
            g = self.games.get(e.game_id)
            hint = g.extra.get("launch_exe_hint") if g else None
        return rank(find_executables(root), root if root.is_dir() else root.parent, e.title, hint)

    def set_executable(self, entry_id: int, exe: Path | str) -> LibraryEntry:
        e = self.get(entry_id)
        exe = Path(exe)
        if not exe.is_file():
            raise LibraryError(f"{exe} is not a file")
        st = e.setup or {}
        if st.get("state") in ("pending", "installing") and st.get("root") and entry_id not in self._installers \
                and not is_within(exe, st["root"]):
            # the repack was installed some other way (its setup.exe run by hand): the game is there now
            game_dir = exe.parent
            for _ in range(3):                         # Game/bin/x.exe, Game/Binaries/Win64/x.exe → Game
                if game_dir.name.lower() in ("bin", "bin64", "bin32", "binaries", "win64", "win32", "x64", "x86",
                                             "system", "engine") and game_dir.parent != game_dir:
                    game_dir = game_dir.parent
            e.setup = {**st, "state": "installed", "installed_to": str(game_dir), "installed_at": time.time(),
                       "manual": True}
            if not e.install_path or is_within(e.install_path, st["root"]):
                e.install_path = str(game_dir)
        e.executable = str(exe)
        e.executable_trusted = False         # a new executable must be confirmed again
        if not e.install_path:
            e.install_path = str(exe.parent)
        self.repo.update(e)
        self._auto_onlinefix_overrides(e)
        self.emit("library_changed", e.id)
        return self.get(e.id)

    AUTO_EXE_MARGIN = 20.0

    def auto_executable(self, entry_id: int) -> str | None:
        """After extracting: pick the game's executable when one clearly stands out, so Play works straight away.
        Nothing is run (Play still asks once before the first start). Returns the file name, or None when unsure."""
        try:
            e = self.get(entry_id)
            if e.executable and Path(e.executable).is_file() and \
                    (not e.install_path or is_within(e.executable, e.install_path)):
                e.status = LibraryStatus.INSTALLED       # chosen before (e.g. a fix put into a Steam game): kept
                self.repo.update(e)
                self._auto_onlinefix_overrides(e)
                self.emit("library_changed", e.id)
                return Path(e.executable).name
            cands = [c for c in self.candidates(entry_id) if c.kind in ("game", "launch script")]
            if not cands or cands[0].kind != "game" or cands[0].score <= 0:
                return None
            if len(cands) > 1 and cands[0].score - cands[1].score < self.AUTO_EXE_MARGIN:
                return None                              # two close candidates: the user chooses
            self.mark_installed(entry_id, executable=cands[0].path)
        except Exception as exc:
            log.info("auto-choosing the executable for entry %s failed: %s", entry_id, exc)
            return None
        log.info("auto-chose %s for library entry %s (score %.0f)", cands[0].path, entry_id, cands[0].score)
        return cands[0].path.name

    def mark_installed(self, entry_id: int, install_path: Path | str | None = None,
                       executable: Path | str | None = None) -> LibraryEntry:
        e = self.get(entry_id)
        if install_path:
            p = Path(install_path)
            if not p.is_dir():
                raise LibraryError(f"{p} is not a folder")
            e.install_path = str(p)
        if executable:
            self.set_executable(entry_id, executable)
            e = self.get(entry_id)
        if not e.install_path:
            raise LibraryError("Choose the installation folder first.")
        e.status = LibraryStatus.INSTALLED
        self.repo.update(e)
        self.emit("library_changed", e.id)
        return e

    def allow_execute(self, entry_id: int) -> None:
        """Set the executable bit on a native Linux binary (only after explicit user consent)."""
        e = self.get(entry_id)
        if not e.executable:
            raise LibraryError("No executable selected.")
        p = Path(e.executable)
        os.chmod(p, p.stat().st_mode | 0o100)

    # --------------------------------------------------------------- launch
    def _proton_name_for(self, e: LibraryEntry, plan) -> str | None:
        if not plan.description or not e.executable or not e.executable.lower().endswith((".exe", ".bat", ".msi")):
            return None
        p = self._proton_for(e)
        return p.name if p is not None and plan.description.startswith(p.name) else None

    def setup_snapshot(self, e: LibraryEntry, plan) -> str:
        """The exact setup of a launch, one line in the game's log: the assistant compares runs that worked with
        ones that failed (ezgd.library.history)."""
        import json as _json
        from ezgd.library import history
        p = self._proton_for(e) if self._proton_name_for(e, plan) else None
        build = None
        if p is not None:
            try:
                build = (Path(p.path) / "version").read_text("utf-8", "replace").strip()[:80] or None
            except OSError:
                build = None
        cfg = e.compat_config
        d = plan.description or ""
        snap = {"proton": p.name if p else None, "proton_build": build, "executable": e.executable,
                "launch_args": e.launch_args or "", "fixes": sorted(cfg.fixes), "options": sorted(cfg.options),
                "dll_overrides": cfg.dll_overrides, "use_runtime": cfg.use_runtime, "protonfixes": cfg.protonfixes,
                "through_steam": "through Steam" in d,
                "overlay": "full" if ("Shift+Tab" in d or "overlay loaded" in d) else
                "friends list" if "friends list only" in d else "off",
                "kernel": os.uname().release if hasattr(os, "uname") else None}
        return history.SETUP_MARK + _json.dumps(snap, separators=(",", ":"))

    def pin_current_protons(self) -> int:
        """Before a new Proton is added: every Windows game without its own choice keeps the Proton it uses now
        (a new GE-Proton can sort first and would otherwise become every game's Proton)."""
        protons = self.protons(refresh=True)
        default = (self.settings.settings.proton_version or (protons[0].name if protons else None))
        if not default:
            return 0
        n = 0
        for e in self.repo.all():
            if not e.proton_version and e.executable and e.executable.lower().endswith((".exe", ".bat", ".msi")):
                e.proton_version = default
                self.repo.update(e)
                n += 1
        if n:
            log.info("kept %s for %d game(s) before installing a new Proton", default, n)
        return n

    WORKED_SECONDS = 120          # a game that ran this long with a Proton keeps that Proton

    def remember_working_proton(self, e: LibraryEntry, proton: str | None, played: int) -> None:
        """Games with no Proton chosen keep the one they worked with: installing a newer Proton (e.g. GE-Proton
        from Settings, which may sort first) must not silently change a game that works."""
        if proton and not e.proton_version and played >= self.WORKED_SECONDS:
            e.proton_version = proton
            log.info("%s worked with %s — kept for it", e.title, proton)

    def overlay_available(self) -> str | None:
        """Why the Steam overlay will not be in the next game (``None``: it will be)."""
        if not self.settings.settings.steam_overlay:
            return "switched off"
        if not IS_LINUX:
            return "only on Linux"
        if steam.overlay_env(self.steam_root()) is None:
            return "Steam is not installed"
        if not steam.is_steam_running():
            return "start Steam first"
        return None

    def _add_overlay(self, plan: launcher.LaunchPlan, e: LibraryEntry | None = None) -> None:
        """The Steam overlay in a game started by the app: Steam's own overlay libraries, no shortcut needed."""
        if e is not None and "no_steam_overlay" in e.compat_config.fixes:
            return                               # switched off for this game on the Fixes screen
        if plan.description.startswith("via Steam") or "gameoverlayrenderer" in os.environ.get("LD_PRELOAD", ""):
            return                               # Steam starts it (shortcut / play mode): Steam adds it itself
        if self.overlay_available() is not None:
            return
        root = self.steam_root()
        env = steam.overlay_env(root, plan.env.get("LD_PRELOAD") or os.environ.get("LD_PRELOAD", ""))
        if env:
            full = False
            fake = self.onlinefix_app_id(e) if e is not None and self.talks_to_steam_itself(e) else None
            if fake:                                 # OnlineFix: the app it plays as (see onlinefix_app_id)
                env.update(SteamGameId=str(fake), SteamOverlayGameId=str(fake))
                full = True
            elif e is not None and e.executable and root is not None and not self.talks_to_steam_itself(e):
                exe, opts, appid = self._runner_shortcut(e)
                if steam.has_shortcut(root, appid, exe, opts):   # Steam knows this game: the full overlay
                    gid = str(steam.shortcut_rungameid(appid))
                    env.update(SteamGameId=gid, SteamOverlayGameId=gid)
                    full = True
            plan.env = {**plan.env, **env}
            plan.description += " · Steam overlay (Shift+Tab)" if full else " · Steam overlay (friends list only)"

    def launch_plan(self, entry_id: int, overrides: dict | None = None) -> launcher.LaunchPlan:
        """Build the command for an entry; ``overrides`` previews unsaved Properties changes."""
        e = self.get(entry_id)
        o = overrides or {}
        exe = o.get("executable", e.executable)
        if not exe:
            raise LibraryError("Select the game's executable first.")
        method = LaunchMethod(o.get("launch_method", e.launch_method))
        app_id = o.get("steam_app_id", e.steam_app_id)
        if "compat" in o:
            e.compat = CompatConfig.from_dict(o["compat"]).to_dict()
        e.executable = exe
        fx = fixes.effects(self._context(e))
        of_env = self.onlinefix_env(e)
        if of_env:
            fx.env = {**of_env, **(fx.env or {})}
        return launcher.build_plan(
            Path(exe), method=method, args=o.get("launch_args", e.launch_args), protons=self.protons(),
            proton_name=o.get("proton_version", e.proton_version) or self.settings.settings.proton_version or None,
            steam_root=self.steam_root(), prefix_dir=Path(o["prefix_dir"]) if o.get("prefix_dir") else
            self.prefix_dir(e.id),
            steam_shortcut_appid=e.steam_shortcut_appid, steam_app_id=int(app_id) if app_id else None,
            compat=e.compat_config, game_root=Path(e.install_path) if e.install_path else None,
            log_dir=self.paths.logs, fix_env=fx.env, fix_wrappers=fx.wrappers, fix_args=fx.args,
            fix_notes=fx.notes)

    # ---------------------------------------------------------------- fixes
    def _context(self, e: LibraryEntry) -> fixes.GameContext:
        exe = Path(e.executable) if e.executable and Path(e.executable).is_file() else None
        return fixes.GameContext(e, exe, advisor._log_text(self.prefix_dir(e.id)) if exe else "")

    def fixes_for(self, entry_id: int) -> list[dict]:
        e = self.get(entry_id)
        ctx = self._context(e)
        return [s.as_dict() for s in fixes.catalog(ctx)]

    def set_fixes(self, entry_id: int, enabled: list[str]) -> LibraryEntry:
        e = self.get(entry_id)
        cfg = fixes.apply_selection(self._context(e), set(enabled))
        return self.update(entry_id, compat=cfg.to_dict())

    def enable_fix(self, entry_id: int, key: str) -> LibraryEntry:
        current = {s["key"] for s in self.fixes_for(entry_id) if s["enabled"]}
        return self.set_fixes(entry_id, sorted(current | {key}))

    def preview(self, entry_id: int, overrides: dict | None = None) -> dict:
        try:
            plan = self.launch_plan(entry_id, overrides)
        except (LibraryError, launcher.LaunchError) as exc:
            return {"ok": False, "error": str(exc), "warnings": []}
        args = (overrides or {}).get("launch_args", self.get(entry_id).launch_args)
        why = self.overlay_available()
        if "no_steam_overlay" in self.get(entry_id).compat_config.fixes:
            why = "off for this game"
        if self.settings.settings.steam_overlay and IS_LINUX:
            plan.description += " · Steam overlay" + (f" ({why})" if why else "")
        return {"ok": True, "description": plan.description, "command": launcher.display_command(plan),
                "warnings": launcher.launch_option_warnings(args)}

    # Wine debug channels for a diagnostic launch: errors from every component, DLL loading (module addresses
    # for crash locations) and exceptions — enough to explain a crash without flooding the log
    DIAGNOSTIC_WINEDEBUG = "err+all,warn+module,+loaddll,+seh,fixme-all"

    def launch(self, entry_id: int, confirmed: bool, diagnostic: bool = False) -> launcher.LaunchPlan:
        """Start the game. ``confirmed`` must be True when the executable is not trusted yet.

        ``diagnostic`` adds Wine's error/DLL/exception logging for the debugging assistant (same launch otherwise).
        """
        e = self.get(entry_id)
        if not e.executable_trusted and not confirmed:
            raise LibraryError("Launching this executable needs your confirmation first.")
        if self.is_running(entry_id):
            raise LibraryError(f"{e.title} is already running.")
        switched = self.ensure_prefix_for(e)
        if switched:
            self.emit("notify", ("Separate prefix for this Proton", switched))
        plan = self.launch_plan(entry_id)
        self._add_overlay(plan, e)
        self._launch_info[entry_id] = {"proton": self._proton_name_for(e, plan),
                                       "overlay": "Steam overlay" in plan.description}
        if diagnostic:
            plan.env = {**plan.env, "WINEDEBUG": self.DIAGNOSTIC_WINEDEBUG}
            plan.description += " · diagnostic run"
        log_path = self.paths.logs / f"game-{e.id}.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {plan.description}\n"
                     f"{self.setup_snapshot(e, plan)}\n{launcher.display_command(plan)}\n")
        if plan.pre:
            self.emit("game_state", {"id": e.id, "running": False, "preparing": True})
            launcher.run_pre_launch(plan, log_path)       # raises LaunchError if an `&&` step fails
        proc = launcher.spawn(plan, log_path)
        self._processes[e.id] = (proc, time.monotonic(), plan, log_path)
        e.executable_trusted = True
        e.last_played_at = time.time()
        e.play_count += 1
        self.repo.update(e)
        self.emit("game_state", {"id": e.id, "running": True})
        self.emit("library_changed", e.id)
        return plan

    # ------------------------------------------------------- running games
    def is_running(self, entry_id: int) -> bool:
        item = self._processes.get(entry_id)
        if item is not None and item[0].poll() is None:
            return True
        return steamrun.read_marker(self.running_dir, entry_id) is not None

    def running_ids(self) -> list[int]:
        return sorted({i for i in self._processes if self.is_running(i)} | set(steamrun.sessions(self.running_dir)))

    @property
    def running_dir(self) -> Path:
        return self.paths.data / "running"

    def _reap_steam(self) -> list[int]:
        """Games started through Steam: notice when they start and stop (play mode records the playtime)."""
        now = steamrun.sessions(self.running_dir)
        for eid, info in now.items():
            if eid not in self._steam_seen:
                self._steam_seen[eid] = info
                self.emit("game_state", {"id": eid, "running": True, "steam": True})
        ended = [eid for eid in self._steam_seen if eid not in now]
        for eid in ended:
            info = self._steam_seen.pop(eid)
            played = int(time.time() - float(info.get("started") or time.time()))
            if self.on_game_exit is not None:
                from ezgd.library import history
                try:
                    rs = history.runs(self.paths.logs / f"game-{eid}.log", limit=1)
                    self.on_game_exit(eid, rs[-1].exit_code if rs else None, played)
                except Exception:
                    log.exception("game exit follow-up failed")
            self.emit("game_state", {"id": eid, "running": False, "played": played, "steam": True})
            self.emit("library_changed", eid)
            self._auto_backup(eid, played)
        return ended

    def last_run_status(self, entry_id: int) -> dict:
        """How the last start went, for the Library's "it did not start — Diagnose" hint."""
        from ezgd.library import history
        log_path = self.paths.logs / f"game-{entry_id}.log"
        rs = history.runs(log_path, limit=1)
        if not rs or self.is_running(entry_id):
            return {"failed": False}
        r = rs[-1]
        e = self.get(entry_id)
        failed = r.failed and not self._reached_game(e, log_path)
        meaning = history.explain_exit(r.exit_code)
        return {"failed": failed, "seconds": r.seconds, "exit_code": r.exit_code, "started": r.started,
                "meaning": meaning[1] if meaning else None}

    def _reached_game(self, e: LibraryEntry, log_path: Path) -> bool:
        """The last run's logs show the game had loaded (so a quick exit was the player closing it)."""
        from ezgd.library import diagnose, history
        try:
            rs = history.runs(log_path, limit=1)
            text = rs[-1].text if rs else ""
            if e.executable:
                exe = Path(e.executable)
                name, eng = diagnose.engine_logs(exe, self.prefix_dir(e.id), diagnose.engine_of(exe))
                if name and time.time() - Path(name).stat().st_mtime < 600:     # written by this run
                    text += "\n" + eng[-200000:]
            return "game loaded" in history.phase(text).reached
        except Exception:
            return False

    def reap(self) -> list[int]:
        """Record playtime for games that exited. Called periodically by the core."""
        finished = self._reap_steam()
        for entry_id, (proc, started, plan, log_path) in list(self._processes.items()):
            code = proc.poll()
            if code is None:
                continue
            del self._processes[entry_id]
            if entry_id in self._installers:
                self._installer_exited(entry_id, self._installers.pop(entry_id), code, log_path)
                finished.append(entry_id)
                continue
            if plan.post:
                threading.Thread(target=launcher.run_post_exit, args=(plan, code, log_path),
                                 name=f"post-exit-{entry_id}", daemon=True).start()
            played = int(time.monotonic() - started)
            try:                                       # the debugging assistant reads this back
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n=== exited with code {code} after {played}s\n")
            except OSError:
                pass
            if self.on_game_exit is not None:
                try:
                    self.on_game_exit(entry_id, code, played)
                except Exception:
                    log.exception("game exit follow-up failed")
            e = self.repo.get(entry_id)
            info = self._launch_info.pop(entry_id, {})
            if e is not None:
                e.playtime_seconds += played
                e.last_played_at = time.time()
                self.remember_working_proton(e, info.get("proton"), played)
                if info.get("overlay") and played < 20 and code not in (0, None) and \
                        "no_steam_overlay" not in e.compat_config.fixes and not self._reached_game(e, log_path):
                    # quit within seconds with Steam's overlay loaded: try the next start without it
                    cfg = e.compat_config
                    cfg.fixes = sorted(set(cfg.fixes) | {"no_steam_overlay"})
                    e.compat = cfg.to_dict()
                    self.emit("notify", ("Steam overlay switched off for this game",
                                         f"{e.title} stopped after {played}s with the Steam overlay loaded, so the next "
                                         "start is without it (Fixes → Steam overlay to turn it back on)."))
                self.repo.update(e)
            log.info("game %s exited with code %s after %ss", entry_id, code, played)
            if e is not None:
                self._auto_backup(entry_id, played)
            self.emit("game_state", {"id": entry_id, "running": False, "exit_code": code, "played": played})
            if code != 0 and played < 15:
                self.emit("notify", ("Game exited immediately",
                                     f"{e.title if e else 'The game'} stopped with code {code}. "
                                     f"Press Diagnose on its Library page to find out why."))
            self.emit("library_changed", entry_id)
            finished.append(entry_id)
        return finished

    def stop(self, entry_id: int) -> None:
        item = self._processes.get(entry_id)
        if (item is None or item[0].poll() is not None) and steamrun.stop(self.running_dir, entry_id):
            return                                   # started through Steam: play mode stops it
        if item is None or item[0].poll() is not None:
            raise LibraryError("The game is not running.")
        launcher.terminate(item[0])
        self.reap()

    def _game_pids(self, entry_id: int) -> tuple[list[int], list[int]]:
        """(process groups, single processes) that belong to this running game: the group the app (or play mode)
        started it in, and every process working on the game's own prefix (Wine can leave the group)."""
        groups, pids = [], []
        item = self._processes.get(entry_id)
        if item is not None and item[0].poll() is None:
            groups.append(item[0].pid)
        info = steamrun.read_marker(self.running_dir, entry_id)
        if info and int(info.get("game") or 0) > 0:
            groups.append(int(info["game"]))
        if IS_LINUX:
            pids = _prefix_processes(self.prefix_dir(entry_id))
        return groups, pids

    @staticmethod
    def _alive(groups: list[int], pids: list[int]) -> bool:
        import signal as _signal
        for g in groups:
            try:
                os.killpg(g, 0)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for p in pids:
            try:
                os.kill(p, 0)
                if Path(f"/proc/{p}/stat").is_file() and \
                        Path(f"/proc/{p}/stat").read_text().split(") ")[-1].startswith("Z"):
                    continue                              # a zombie is gone (its parent collects it)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        _ = _signal
        return False

    def stop_completely(self, entry_id: int, grace: float = 10.0) -> dict:
        """Close a game for good: ask it to quit (SIGTERM — it can still save), wait up to ``grace`` seconds, then
        force-stop whatever is left of it (SIGKILL), including Wine processes of its prefix that left the game's
        process group. Only the game's own processes are touched. Returns {"stopped": bool, "left": [pids]}."""
        import signal as _signal
        if os.name == "nt":
            item = self._processes.get(entry_id)
            if item is not None:
                launcher.terminate(item[0])
            steamrun.stop(self.running_dir, entry_id)
            self.reap()
            return {"stopped": not self.is_running(entry_id), "left": []}
        groups, pids = self._game_pids(entry_id)
        if not groups and not pids and not self.is_running(entry_id):
            return {"stopped": True, "left": []}
        log.info("stopping game %s completely (groups %s, prefix processes %s)", entry_id, groups, pids)
        for g in groups:
            try:
                os.killpg(g, _signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for p in pids:
            try:
                os.kill(p, _signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            self.reap()
            g2, p2 = self._game_pids(entry_id)
            groups = sorted(set(groups) | set(g2))
            pids = sorted(set(pids) | set(p2))
            if not self._alive(groups, pids):
                break
            time.sleep(0.25)
        else:
            log.warning("game %s did not quit within %ss — force-stopping it", entry_id, grace)
            for _round in range(3):
                g2, p2 = self._game_pids(entry_id)
                for g in sorted(set(groups) | set(g2)):
                    try:
                        os.killpg(g, _signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                for p in sorted(set(pids) | set(p2)):
                    try:
                        os.kill(p, _signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                time.sleep(0.5)
                self.reap()
                if not self._alive(sorted(set(groups) | set(g2)), sorted(set(pids) | set(p2))):
                    break
        for _ in range(20):                          # play mode removes its marker when the game has gone
            self.reap()
            if not self.is_running(entry_id):
                break
            time.sleep(0.25)
        g3, p3 = self._game_pids(entry_id)
        left = [p for p in sorted(set(p3)) if self._alive([], [p])] + [g for g in g3 if self._alive([g], [])]
        stopped = not left and not self.is_running(entry_id)
        if not stopped:
            log.error("game %s: processes left after stopping: %s", entry_id, left)
        return {"stopped": stopped, "left": left}

    def stop_all(self) -> None:
        for entry_id in self.running_ids():
            try:
                launcher.terminate(self._processes[entry_id][0])
            except Exception:
                log.exception("could not stop game %s", entry_id)
        self.reap()

    # ------------------------------------------------------- compatibility
    def suggest_overrides(self, entry_id: int, exe: str | None = None) -> dict:
        e = self.get(entry_id)
        exe = exe or e.executable
        if not exe:
            raise LibraryError("Select the game's executable first.")
        s = suggest_dll_overrides(Path(exe))
        return {"overrides": s.overrides, "found": s.found, "onlinefix": s.onlinefix, "explanation": s.explanation}

    def suggestions(self, entry_id: int) -> list[dict]:
        e = self.get(entry_id)
        g = self.games.get(e.game_id) if e.game_id else None
        return [s.__dict__ for s in advisor.suggestions(e, self.prefix_dir(entry_id), g.extra if g else None)]

    def apply_suggestion(self, entry_id: int, key: str) -> LibraryEntry:
        match = next((s for s in self.suggestions(entry_id) if s["key"] == key), None)
        if match is None:
            raise LibraryError("That suggestion no longer applies.")
        if not match["changes"]:
            raise LibraryError("This suggestion is information only.")
        changes = dict(match["changes"])
        fix = changes.pop("enable_fix", None)
        if fix:
            self.enable_fix(entry_id, fix)
        script = changes.pop("set_executable", None)
        if script:
            self.set_executable(entry_id, script)
        return self.update(entry_id, **changes) if changes else self.get(entry_id)

    def _auto_onlinefix_overrides(self, e: LibraryEntry) -> None:
        """OnlineFix's fix is loaded through a proxy DLL (winmm.dll etc.) that Wine replaces with its own
        built-in unless overridden — without it the game fails Steam initialisation. Applied automatically
        (visible and editable in Properties → Compatibility) because the fix cannot work otherwise."""
        _OF_INI_CACHE.clear()                     # called after extracting / applying a fix: files changed
        if not (IS_LINUX and e.executable and launcher.is_windows_binary(Path(e.executable))):
            return
        missing, onlinefix, _ = advisor._missing_overrides(e)
        if onlinefix and missing and "game_dlls" not in e.compat_config.fixes:
            cfg = e.compat_config
            cfg.fixes = sorted(set(cfg.fixes) | {"game_dlls"})
            e.compat = cfg.to_dict()
            self.repo.update(e)
            log.info("%s: enabled the 'game DLLs' fix for OnlineFix (%s)", e.title, ", ".join(missing))

    def protonfix_status(self, entry_id: int, proton_name: str | None = None, app_id: int | None = None) -> str:
        e = self.get(entry_id)
        protons = self.protons()
        p = next((x for x in protons if x.name == proton_name), None) or self._proton_for(e)
        app_id = app_id if app_id is not None else e.steam_app_id
        if p is None:
            return "No Proton installed."
        if not p.has_protonfixes:
            return f"{p.name} does not include protonfixes (Valve builds rely on SteamAppId-based tweaks instead)."
        if not app_id:
            return f"{p.name} includes protonfixes, but without a Steam App ID only the default fixes apply."
        if p.has_fix_for(app_id):
            return f"{p.name} has a dedicated protonfix for Steam app {app_id}; it will be applied."
        return f"{p.name} has no specific fix for Steam app {app_id}; the default fixes apply."

    def prefix_info(self, entry_id: int) -> dict:
        d = self.prefix_dir(entry_id)
        return {"path": str(d), "exists": prefix_ready(d),
                "log": str(self.paths.logs / f"game-{entry_id}.log")}

    def prefix_tool(self, entry_id: int, tool: str) -> None:
        """Run winecfg or winetricks against the game's Proton prefix (user-initiated)."""
        e = self.get(entry_id)
        self.ensure_prefix_for(e)                    # install into the prefix this Proton really uses
        prefix = self.prefix_dir(entry_id)
        if not prefix_ready(prefix):
            raise LibraryError("The Proton prefix does not exist yet — launch the game once first.")
        proton = self._proton_for(e)
        if proton is None:
            raise LibraryError("No Proton installation was found.")
        try:
            env = winetricks_env(proton, prefix)
        except FileNotFoundError as exc:
            raise LibraryError(str(exc)) from exc
        if tool == "winecfg":
            argv = [env["WINE"], "winecfg"]
        elif tool == "winetricks":
            wt = shutil.which("winetricks")
            if not wt:
                raise LibraryError("winetricks is not installed.")
            argv = [wt]
        else:
            raise LibraryError(f"unknown tool {tool}")
        launcher.spawn(launcher.LaunchPlan(argv, str(prefix), env, tool), self.paths.logs / f"game-{entry_id}.log")

    # ---------------------------------------------------- debugging assistant
    @property
    def learned_path(self) -> Path:
        return self.paths.data / "learned_fixes.json"

    def steam_reference(self, e: LibraryEntry, game=None) -> dict | None:
        """How Steam itself runs this game, when Steam has (ezgd.library.steamref)."""
        from ezgd.library import steamref
        if not IS_LINUX:
            return None
        app = e.steam_app_id or ((game.extra or {}).get("steam_base_appid") if game else None)
        try:
            return steamref.reference(self.steam_root(), int(app) if app else None, self.protons())
        except Exception as exc:
            log.info("Steam reference for %s failed: %s", e.title, exc)
            return None

    def diagnose(self, entry_id: int, protondb: dict | None = None, with_system: bool = True,
                 symptom: str = "", integrity: dict | None = None, install_dest: str | None = None,
                 community: dict | None = None, evidence: dict | None = None) -> dict:
        from ezgd.library.diagnose import diagnose
        from ezgd.platform import syscheck
        e = self.get(entry_id)
        game = self.games.get(e.game_id) if e.game_id else None
        proton = self._proton_for(e)
        exe_dir = Path(e.executable).parent if e.executable else Path(e.install_path or ".")
        system = syscheck.collect(exe_dir, self.prefix_dir(e.id)) if with_system and IS_LINUX else None
        return diagnose(e, self.prefix_dir(e.id), self.paths.logs / f"game-{e.id}.log", protons=self.protons(),
                        proton_name=proton.name if proton else "", game_extra=game.extra if game else {},
                        system=system, protondb=protondb, symptom=symptom,
                        learned=fixgen.load_learned(self.learned_path), integrity=integrity,
                        methods=[m.to_dict() for m in (game.download_methods or [])] if game else [],
                        install_dest=Path(install_dest) if install_dest else None, community=community,
                        steam_ref=self.steam_reference(e, game), evidence=evidence).to_dict()

    # --------------------------------------------------------------- trials
    TRIAL_SECONDS = 45
    MAX_TRIALS = 6

    MAX_DEEP_TRIALS = 18

    def trial_candidates(self, entry_id: int, findings: list[dict], generated: list[dict] | None = None,
                         deep: bool = False) -> list[dict]:
        """What "Try fixes for me" will test: the current setup, the recommended fixes, other Proton versions.
        ``deep`` ("Try everything"): every generic fix too, up to MAX_DEEP_TRIALS setups."""
        e = self.get(entry_id)
        current = (self._proton_for(e).name if self._proton_for(e) else "")
        cands = [{"label": f"Current setup ({current or 'Proton'})", "overrides": {}}]
        cfg = e.compat_config.to_dict()

        def with_option(opt: str) -> dict:
            c = dict(cfg)
            c["options"] = sorted(set(c.get("options") or []) | {opt})
            return {"compat": c}
        for f in findings:
            fx = f.get("fix") or {}
            if fx.get("kind") == "enable_fix" and fx["value"].startswith("opt:"):
                cands.append({"label": fx["label"], "overrides": with_option(fx["value"][4:])})
            elif fx.get("kind") == "enable_fix":
                c = dict(cfg)
                c["fixes"] = sorted(set(c.get("fixes") or []) | {fx["value"]})
                cands.append({"label": fx["label"] if fx["label"] != "Apply" else f["title"], "overrides": {"compat": c}})
            elif fx.get("kind") == "add_args":
                cands.append({"label": fx["label"], "overrides": {
                    "launch_args": advisor.with_game_args(e.launch_args or "", fx["value"])}})
            elif fx.get("kind") == "proton":
                cands.append({"label": fx["label"], "overrides": {"proton_version": fx["value"]}})
            elif fx.get("kind") == "winetricks" and f.get("confidence", 0) >= 0.5 and not \
                    any(v.startswith("dotnet") for v in fx["value"].split()):
                # proven in a test copy first (.NET installs take far too long for a test)
                cands.append({"label": fx["label"] + " (tested in a copy)",
                              "overrides": {"winetricks": fx["value"].split()}})
            elif fx.get("kind") == "restore_setup":   # what worked before is the best bet: tested first
                import json as _json
                spec = _json.loads(fx["value"] or "{}")
                ov = {k: v for k, v in spec.items() if k in ("proton_version", "launch_args", "compat", "executable")}
                if ov:
                    cands.insert(1, {"label": fx["label"], "overrides": ov})
        for p in self.protons():
            if p.name != current and not any(c["overrides"].get("proton_version") == p.name for c in cands):
                cands.append({"label": f"Use {p.name}", "overrides": {"proton_version": p.name}})
        graphics = any(k in (f.get("key") or "") for f in findings for k in ("graphics", "vulkan", "dx12", "dump-"))
        if (graphics or deep) and "wined3d" not in (cfg.get("options") or []) and \
                not any("wined3d" in str(c["overrides"]) for c in cands):
            cands.append({"label": "Use WineD3D instead of DXVK", "overrides": with_option("wined3d")})
        # the assistant's own fixes (learned ones first, already sorted by the generator)
        gen = list(generated or [])
        for g in gen:
            cands.insert(1 if g.get("learned_from") else len(cands), {
                "label": g["label"] + (f" (worked for {g['learned_from']})" if g.get("learned_from") else ""),
                "overrides": g["overrides"], "gen_id": g["gen_id"], "generated": True})
        seen, out = set(), []
        for c in cands:
            k = repr(sorted((k, repr(v)) for k, v in c["overrides"].items()))
            if k not in seen:
                seen.add(k)
                out.append(c)
        if deep:
            return out[:self.MAX_DEEP_TRIALS]
        return out[:self.MAX_TRIALS + (2 if gen else 0)]

    MAX_PAIRS = 3

    def combine_candidates(self, results: list[dict]) -> list[dict]:
        """Pairs of single setups that each failed, for games that need two things at once. The ones that got
        furthest (survived longest) are combined first; conflicting pairs are skipped."""
        singles = [r for r in results if r.get("overrides") and r.get("outcome") in ("crashed", "exited")]
        singles.sort(key=lambda r: (-len(r.get("progress") or []), -(r.get("seconds") or 0)))   # got furthest first
        top = singles[:4]
        pairs = []
        for i in range(len(top)):
            for j in range(i + 1, len(top)):
                merged = merge_overrides(top[i]["overrides"], top[j]["overrides"])
                if merged is not None:
                    pairs.append({"label": f"{top[i]['label']} + {top[j]['label']}", "overrides": merged,
                                  "combined": True})
                if len(pairs) >= self.MAX_PAIRS:
                    return pairs
        return pairs

    def _trial_prefix(self, entry_id: int, n: int, proton_name: str | None = None) -> tuple[Path, str]:
        """A throw-away copy of the game's prefix (instant reflink copy where the filesystem supports it) — or of
        the prefix of the tested Proton's kind; a fresh one when that Proton cannot use either."""
        src = self.prefix_source_for(entry_id, proton_name)
        dst = self.paths.prefixes / f"trial-{entry_id}-{n}"
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        if src is not None and prefix_ready(src):
            r = subprocess.run(["cp", "-a", "--reflink=always", str(src), str(dst)], capture_output=True, timeout=120)
            if r.returncode == 0:
                return dst, "a copy of the game's prefix"
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True)
        return dst, "a fresh prefix (your drive cannot make instant copies)"

    def run_trial(self, entry_id: int, cand: dict, n: int, cancel: threading.Event | None = None) -> dict:
        """Launch the game once with ``cand`` in a test prefix; report whether it crashed, exited or kept running."""
        if self.is_running(entry_id):
            raise LibraryError("Stop the game first.")
        prefix, how = self._trial_prefix(entry_id, n, cand["overrides"].get("proton_version"))
        log_path = self.paths.logs / f"trial-{entry_id}.log"
        started = time.monotonic()
        result = {"label": cand["label"], "overrides": cand["overrides"], "prefix": how}
        edited = []
        try:
            edits = cand["overrides"].get("edits") or []
            if edits:                                  # config edits: on the trial copy, or undone right after
                edited = fixgen.apply_edits(edits, (self.prefix_dir(entry_id), prefix))
            verbs = cand["overrides"].get("winetricks") or []
            if verbs:                                  # components / Windows version: into the copy, then test
                if not prefix_ready(prefix):
                    result.update(outcome="error", seconds=0, detail="needs the game's own prefix — start the game "
                                                                     "once first")
                    return result
                code = self._winetricks_into(entry_id, verbs, prefix, log_path, cand["overrides"].get("proton_version"))
                if code != 0:
                    result.update(outcome="error", seconds=0, detail=f"installing {' '.join(verbs)} into the test copy "
                                  f"failed: {self._winetricks_failure(entry_id, ' '.join(verbs), log_path)}")
                    return result
            plan = self.launch_plan(entry_id, {**cand["overrides"], "prefix_dir": str(prefix)})
            plan.env = {**plan.env, "WINEDEBUG": self.DIAGNOSTIC_WINEDEBUG}
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} trial {n}: {cand['label']} · diagnostic run\n")
                offset = fh.tell()
            proc = launcher.spawn(plan, log_path)
            code = None
            while time.monotonic() - started < self.TRIAL_SECONDS:
                if cancel is not None and cancel.is_set():
                    break
                code = proc.poll()
                if code is not None:
                    break
                time.sleep(0.5)
            secs = int(time.monotonic() - started)
            still = proc.poll() is None
            if still:
                launcher.terminate(proc)
            try:
                with open(log_path, "rb") as fh:
                    fh.seek(offset)
                    text = fh.read(8 * 1024 * 1024).decode("utf-8", "replace")
            except OSError:
                text = ""
            reason = _trial_reason(text)
            frames = _drew_frames(text)
            # how far it got: Wine's log plus the engine's own log (written inside the test prefix, or in the
            # game folder during this test)
            from ezgd.library import diagnose as _dg
            from ezgd.library import history as _hist
            eng = ""
            try:
                exe = Path(cand["overrides"].get("executable") or self.get(entry_id).executable or "")
                name, eng = _dg.engine_logs(exe, prefix, _dg.engine_of(exe))
                if name and Path(name).stat().st_mtime < time.time() - secs - 5:
                    eng = ""                              # older than this test
            except Exception:
                eng = ""
            progress = _hist.phase(text + "\n" + eng).reached
            result["progress"] = progress
            # the test's detailed Wine log is evidence the assistant analyses next (investigate / adaptive trials)
            keep = text if len(text) < 1536 * 1024 else text[:512 * 1024] + "\n…\n" + text[-1024 * 1024:]
            ev = self.trial_evidence.setdefault(entry_id, {})
            ev[n] = {"text": keep, "exit_code": None if still else code, "seconds": secs, "label": cand["label"],
                     "overrides": cand["overrides"]}
            for old_n in sorted(ev)[:-8]:              # loader errors are early, crashes late: head + tail kept
                del ev[old_n]
            result["frames"] = frames
            if cancel is not None and cancel.is_set() and still:
                result.update(outcome="cancelled", seconds=secs, detail="stopped by you")
            elif still:
                stuck = frames is not True and not any(m in progress for m in ("game window opened", "game loaded"))
                result.update(outcome="ran", seconds=secs, stuck=stuck,
                              detail=f"kept running for {secs}s without crashing"
                              + {True: ", drawing frames", False: ", but drew no frames (black screen?)",
                                 None: ""}[frames]
                              + (" — got to the game" if "game loaded" in progress else
                                 " — but no sign it got past start-up" if stuck and progress else "")
                              + (f" ({reason})" if reason else ""))
            elif reason:
                result.update(outcome="crashed", seconds=secs, detail=f"stopped after {secs}s: {reason}"
                              + (f" (got to: {progress[-1]})" if progress else ""))
            else:
                result.update(outcome="exited", seconds=secs, detail=f"exited with code {code} after {secs}s"
                              + (f" (got to: {progress[-1]})" if progress else ""))
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"=== trial result: {result['outcome']} — {result['detail']}\n")
        except (LibraryError, launcher.LaunchError) as exc:
            result.update(outcome="error", seconds=0, detail=str(exc))
        finally:
            fixgen.revert_edits(edited)
            _stop_prefix_processes(prefix)
            for _ in range(5):                       # wineserver may still flush the registry on its way out
                shutil.rmtree(prefix, ignore_errors=True)
                if not prefix.exists():
                    break
                time.sleep(1)
        return result

    # folders inside a prefix where Windows games keep saves and settings
    SAVE_DIRS = ("AppData/Roaming", "AppData/Local", "AppData/LocalLow", "Documents", "Saved Games")

    # ---------------------------------------------- one prefix per kind of Proton
    def _proton_named(self, e: LibraryEntry, name: str | None = None):
        if name:
            p = next((x for x in self.protons() if x.name == name), None)
            if p is not None:
                return p
        return self._proton_for(e)

    def ensure_prefix_for(self, e: LibraryEntry, proton_name: str | None = None) -> str | None:
        """Before Proton runs: make the game's prefix one this Proton can use (see ezgd.platform.prefixguard). A
        prefix made by another kind of Proton (or a newer build) is put aside as that kind's prefix, this kind's own
        prefix comes back (or a new one is made), and saves move along. Returns a message when it switched."""
        from ezgd.platform import prefixguard as pg
        if not IS_LINUX or not e.executable or not launcher.is_windows_binary(Path(e.executable)):
            return None
        proton = self._proton_named(e, proton_name)
        want = pg.proton_prefix_version(getattr(proton, "path", None))
        cur = self.prefix_dir(e.id)
        if not want:
            return None
        if not cur.exists():
            pg.mark(cur, want)                             # a new prefix: remember who makes it
            return None
        have, damaged = pg.effective_version(cur, self._log_tail(e.id))
        if pg.compatible(have, want) and not damaged:
            if pg.family(have) == pg.family(want) or not have:
                pg.mark(cur, want)                         # (an upgrade within the same kind is Proton's job)
            return None
        if self.is_running(e.id):
            return None
        old_fam, new_fam = pg.family(have) or "unknown", pg.family(want) or "unknown"
        stamp = time.strftime('%Y%m%d-%H%M%S')
        # a prefix a failed upgrade mixed up is kept only as a backup (".old-…"), never used again
        retired = cur.with_name(f"{cur.name}.old-{stamp}" if damaged else pg.slot_name(cur.name, old_fam))
        if retired.exists():                               # an older copy of that kind: keep it, out of the way
            retired.rename(retired.with_name(f"{retired.name}.old-{stamp}"))
        cur.rename(retired)
        back = cur.with_name(pg.slot_name(cur.name, new_fam))
        reused = back.exists() and pg.compatible(pg.effective_version(back)[0], want)
        if reused:
            back.rename(cur)
        else:
            pg.mark(cur, want)
        moved = self._carry_saves(retired, cur)
        msg = (f"{e.title}: {proton.name if proton else 'this Proton'} cannot use the game's prefix (made by "
               f"{have}) — it now uses {'its own earlier' if reused else 'a new'} prefix"
               + (f", with your saves copied over ({moved} files)" if moved else "")
               + ". The other prefix is kept for when you switch back.")
        log.info(msg)
        try:
            with open(self.paths.logs / f"game-{e.id}.log", "a", encoding="utf-8") as fh:
                fh.write(f"\n=== prefix switched: {have} → {want} ({'reused' if reused else 'new'}; "
                         f"{retired.name} kept; {moved} save files copied)\n")
        except OSError:
            pass
        return msg

    def _log_tail(self, entry_id: int, limit: int = 2 * 1024 * 1024) -> str:
        p = self.paths.logs / f"game-{entry_id}.log"
        try:
            size = p.stat().st_size
            with open(p, "rb") as fh:
                fh.seek(max(0, size - limit))
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def _carry_saves(self, src_prefix: Path, dst_prefix: Path) -> int:
        """Saves and settings (AppData, Documents …) from one prefix into another: missing or older files are
        replaced by the newer ones, so progress follows the player between Proton versions."""
        src_user = src_prefix / "pfx" / "drive_c" / "users" / "steamuser"
        dst_user = dst_prefix / "pfx" / "drive_c" / "users" / "steamuser"
        n = 0
        for rel in self.SAVE_DIRS:
            src = src_user / rel
            if not src.is_dir():
                continue
            for f in src.rglob("*"):
                if not f.is_file() or f.is_symlink():
                    continue
                if any(part in ("Temp", "Microsoft", "Packages", "CrashDumps") for part in f.relative_to(src).parts[:1]):
                    continue
                t = dst_user / rel / f.relative_to(src)
                try:
                    if t.exists() and t.stat().st_mtime >= f.stat().st_mtime:
                        continue
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, t)
                    n += 1
                except OSError:
                    continue
        return n

    def prefix_source_for(self, entry_id: int, proton_name: str | None) -> Path | None:
        """For a test run with ``proton_name``: the prefix to copy (the game's own, or that Proton kind's)."""
        from ezgd.platform import prefixguard as pg
        e = self.get(entry_id)
        cur = self.prefix_dir(entry_id)
        want = pg.proton_prefix_version(getattr(self._proton_named(e, proton_name), "path", None))
        have, damaged = pg.effective_version(cur, self._log_tail(entry_id))
        if not damaged and pg.compatible(have, want):
            return cur
        slot = cur.with_name(pg.slot_name(cur.name, pg.family(want) or "unknown"))
        return slot if slot.exists() and pg.compatible(pg.effective_version(slot)[0], want) else None

    def rebuild_prefix(self, entry_id: int) -> str:
        """Move the prefix aside (kept as ``game-N.old-<time>``) so Proton creates a fresh one on the next Play."""
        if self.is_running(entry_id):
            raise LibraryError("Stop the game first.")
        d = self.prefix_dir(entry_id)
        if not d.exists():
            return ""
        self.backup_saves(entry_id, "before-reset", quiet=True)
        if not is_within(d, self.paths.prefixes) or d.resolve() == self.paths.prefixes.resolve():
            raise LibraryError("Refusing to touch a folder outside the app's prefixes.")
        old = d.with_name(f"{d.name}.old-{time.strftime('%Y%m%d-%H%M%S')}")
        d.rename(old)
        log.info("prefix of entry %s moved aside to %s", entry_id, old)
        return str(old)

    # ------------------------------------------------------------ save backups
    def save_folder(self, e: LibraryEntry) -> Path:
        return self.paths.save_backups / saves.key_for(e.title)

    def backup_saves(self, entry_id: int, reason: str = "manual", quiet: bool = False,
                     max_bytes: int | None = None) -> dict | None:
        """Zip this game's saves (see ezgd.library.saves). Returns the backup, or None when there was nothing new.
        ``quiet``: a failure is logged instead of raised (automatic backups must never block anything)."""
        try:
            e = self.get(entry_id)
            pfx = self.prefix_dir(entry_id)
            if not saves.users_dir(pfx).is_dir():
                if quiet:
                    return None
                raise LibraryError("This game has no saves to back up yet: it has its own Proton prefix only after "
                                   "it was started once (Linux).")
            b = saves.backup(pfx, self.save_folder(e), title=e.title, reason=reason, entry_id=e.id,
                             max_bytes=max_bytes, force=reason == "manual")
        except (saves.SaveBackupError, OSError) as exc:
            if quiet:
                log.warning("save backup of entry %s (%s) failed: %s", entry_id, reason, exc)
                return None
            raise LibraryError(str(exc)) from exc
        if b is not None:
            self.emit("saves_changed", entry_id)
        return b.public() if b is not None else None

    def save_backups(self, entry_id: int) -> dict:
        """This game's backups (also those from an earlier install of the same title) and what is in its prefix."""
        e = self.get(entry_id)
        pfx = self.prefix_dir(entry_id)
        files = saves.collect(pfx) if saves.users_dir(pfx).is_dir() else []
        return {"backups": [b.public() for b in saves.list_backups(self.save_folder(e))],
                "folder": str(self.save_folder(e)), "has_prefix": saves.users_dir(pfx).is_dir(),
                "current_files": len(files), "current_bytes": sum(f[2] for f in files),
                "current_folders": saves._folders(files), "supported": IS_LINUX}

    def _own_backup(self, e: LibraryEntry, path: str) -> Path:
        p = Path(path)
        if p.suffix != ".zip" or not is_within(p, self.save_folder(e)) or not p.is_file():
            raise LibraryError("That is not a save backup of this game.")
        return p

    def restore_save_backup(self, entry_id: int, path: str) -> dict:
        """Put a backup's files back into the prefix; the current saves are backed up first (undo)."""
        e = self.get(entry_id)
        if self.is_running(entry_id):
            raise LibraryError("Quit the game first — it would overwrite the restored saves.")
        z = self._own_backup(e, path)
        safety = self.backup_saves(entry_id, "before-restore", quiet=True)
        try:
            n = saves.restore(z, self.prefix_dir(entry_id))
        except saves.SaveBackupError as exc:
            raise LibraryError(str(exc)) from exc
        self.emit("saves_changed", entry_id)
        return {"files": n, "safety": safety}

    def delete_save_backup(self, entry_id: int, path: str) -> None:
        e = self.get(entry_id)
        self._own_backup(e, path).unlink()
        self.emit("saves_changed", entry_id)

    def _auto_backup(self, entry_id: int, played: int) -> None:
        """After a play session: back the saves up in the background (never blocks the reaper)."""
        if played < 60 or not getattr(self.settings.settings, "auto_backup_saves", True) or not IS_LINUX:
            return
        threading.Thread(target=self.backup_saves, args=(entry_id, "auto", True, saves.AUTO_MAX_BYTES),
                         name=f"save-backup-{entry_id}", daemon=True).start()

    def restore_saves(self, entry_id: int, old_prefix: str) -> int:
        """Copy saves/settings from an old prefix into the current one (existing files are kept). Returns files copied."""
        old = Path(old_prefix)
        new = self.prefix_dir(entry_id)
        if not is_within(old, self.paths.prefixes) or not old.name.startswith(f"{new.name}.old-"):
            raise LibraryError("That is not an old prefix of this game.")
        if not prefix_ready(new):
            raise LibraryError("Start the game once first, so the new prefix exists.")
        src_user = old / "pfx" / "drive_c" / "users" / "steamuser"
        dst_user = new / "pfx" / "drive_c" / "users" / "steamuser"
        copied = 0
        for rel in self.SAVE_DIRS:
            src = src_user / rel
            if not src.is_dir():
                continue
            for f in src.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    t = dst_user / rel / f.relative_to(src)
                    if t.exists():
                        continue
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, t)
                    copied += 1
        log.info("restored %d save/setting files for entry %s from %s", copied, entry_id, old)
        return copied

    def apply_diagnosis_fix(self, entry_id: int, kind: str, value: str) -> str:
        """Carry out a fix the assistant recommended; returns what was done (for the UI)."""
        e = self.get(entry_id)
        if kind == "enable_fix":
            if value.startswith("opt:"):
                cfg = e.compat_config
                cfg.options = sorted(set(cfg.options) | {value[4:]})
                e.compat = cfg.to_dict()
                self.repo.update(e)
                self.emit("library_changed", e.id)
            else:
                self.enable_fix(entry_id, value)
            return "Fix switched on — it is used the next time you press Play."
        if kind == "set_executable":
            self.set_executable(entry_id, value)
            return f"Play now starts {Path(value).name} (you confirm it on the next Play)."
        if kind == "add_args":
            self.update(entry_id, launch_args=advisor.with_game_args(e.launch_args or "", value))
            return f"Added {value} to the launch options."
        if kind == "launch_args":
            self.update(entry_id, launch_args=value)
            return "Launch options fixed."
        if kind == "winetricks":
            code = self.run_winetricks(entry_id, value.split(), wait=True)     # the button shows "Installing…"
            if code not in (0, None):
                raise LibraryError(f"Installing {value} failed: " + self._winetricks_failure(entry_id, value)
                                   + " (winetricks' output is in the game's log.)")
            return f"Installed {value} into the game's prefix — press Play to test."
        if kind == "separate_prefix":
            msg = self.ensure_prefix_for(e)
            return (msg + " Press Play to test.") if msg else "The prefix already fits this Proton — press Play."
        if kind == "set_overrides":
            cfg = e.compat_config
            cfg.dll_overrides = value
            self.update(entry_id, compat=cfg.to_dict())
            return "DLL overrides changed — press Play to test."
        if kind == "restore_setup":                 # the setup of a run that worked (ezgd.library.history)
            import json as _json
            spec = _json.loads(value or "{}")
            exe = spec.pop("executable", None)
            changes = {k: v for k, v in spec.items() if k in ("proton_version", "launch_args", "compat")}
            if changes:
                self.update(entry_id, **changes)
            if exe and exe != e.executable:
                self.set_executable(entry_id, exe)
            done = [{"proton_version": "Proton", "launch_args": "launch options", "compat": "compatibility settings"}[k]
                    for k in changes] + (["executable"] if exe else [])
            return ("Back to the setup that worked (" + ", ".join(done) + ") — press Play to test.") if done else \
                "Nothing to change."
        if kind == "proton":
            if not any(p.name == value for p in self.protons()):
                raise LibraryError(f"{value} is not installed.")
            self.update(entry_id, proton_version=value)
            return f"The game now uses {value}."
        if kind == "reset_prefix":
            old = self.rebuild_prefix(entry_id)
            return ("The old prefix was kept as " + Path(old).name + "; a fresh one is created on the next Play. "
                    "Afterwards Diagnose offers to copy your saves back.") if old else "There was no prefix to rebuild."
        if kind == "restore_saves":
            n = self.restore_saves(entry_id, value)
            return f"Copied {n} save/setting file(s) into the new prefix."
        if kind == "command":
            return "Command copied — paste it into a terminal (it asks for your password)."
        if kind in ("write_file", "copy_files", "edit", "copy_folder"):
            return self._file_fix(e, kind, value)
        raise LibraryError(f"unknown fix {kind}")

    def _game_root(self, e: LibraryEntry) -> Path:
        if e.install_path:
            return Path(e.install_path)
        return Path(e.executable).parent if e.executable else Path(".")

    def _inside_game(self, e: LibraryEntry, *paths: str | Path) -> None:
        root = self._game_root(e).resolve()
        for p in paths:
            rp = Path(p).resolve()
            if rp != root and not is_within(rp, root):
                raise LibraryError(f"Refusing to change {p}: it is outside the game's folder.")

    def _file_fix(self, e: LibraryEntry, kind: str, value: str) -> str:
        """File changes the assistant recommends — only inside the game's folder, always with a backup."""
        import json as _json
        from ezgd.installation.integrity import copy_tree_with_backup
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if kind == "write_file":
            spec = _json.loads(value)
            p = Path(spec["path"])
            self._inside_game(e, p)
            if p.exists():
                shutil.copy2(p, p.with_name(f"{p.name}.ezgd-backup-{stamp}"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(spec["content"], "utf-8")
            log.info("%s: wrote %s", e.title, p)
            return f"Wrote {p.name}."
        if kind == "copy_files":
            pairs = _json.loads(value)
            for src, dst in pairs:
                self._inside_game(e, src, dst)
            for src, dst in pairs:
                s, d = Path(src), Path(dst)
                if d.exists():
                    d.rename(d.with_name(f"{d.name}.ezgd-backup-{stamp}"))
                if s.is_dir():
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
            log.info("%s: copied %d item(s) next to the game", e.title, len(pairs))
            return f"Copied {len(pairs)} file(s) next to the game."
        if kind == "edit":
            edits = _json.loads(value)
            for ed in edits:
                self._inside_game(e, ed["path"])
            backups = fixgen.backup_and_apply(edits)
            return "Changed the setting" + (f" (backup: {Path(backups[0]).name})" if backups else "") + "."
        if kind == "copy_folder":
            spec = _json.loads(value)
            self._inside_game(e, spec["src"], spec["dst"])
            n, bdir = copy_tree_with_backup(Path(spec["src"]), Path(spec["dst"]))
            log.info("%s: copied %s over the game (%d files, backup %s)", e.title, spec["src"], n, bdir)
            return f"Copied {n} file(s) into the game" + (f"; replaced files are kept in {bdir.name}" if bdir else "") \
                + "."
        raise LibraryError(f"unknown fix {kind}")

    def run_winetricks(self, entry_id: int, verbs: list[str], wait: bool = False) -> int | None:
        """Install runtimes (e.g. vcrun2022, d3dx9) into the game's prefix — only when a diagnosis asks for them."""
        import re as _re
        if not verbs or not all(_re.fullmatch(r"[a-z0-9_]+", v) for v in verbs):
            raise LibraryError("invalid winetricks verb")
        e = self.get(entry_id)
        prefix = self.prefix_dir(entry_id)
        if not prefix_ready(prefix):
            raise LibraryError("The Proton prefix does not exist yet — launch the game once first.")
        proton = self._proton_for(e)
        wt = shutil.which("winetricks")
        if proton is None or not wt:
            raise LibraryError("winetricks is not installed (Settings → System check → Install missing tools).")
        env = winetricks_env(proton, prefix)
        log_path = self.paths.logs / f"game-{entry_id}.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} winetricks {' '.join(verbs)}\n")
        proc = launcher.spawn(launcher.LaunchPlan([wt, "-q", *verbs], str(prefix), env, "winetricks"), log_path)
        if wait:                                     # "Fix my game" tests the game right after: wait for it
            try:
                return proc.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                launcher.terminate(proc)
                raise LibraryError(f"winetricks {' '.join(verbs)} did not finish within 30 minutes.")
        return None

    _WT_REASONS = [
        (r"(sha256sum mismatch|checksum mismatch|Checksum of .* is wrong)",
         "a downloaded installer was damaged or has changed — press it again; if it keeps failing, winetricks needs "
         "an update (it is fixed on its side)."),
        (r"(Could not resolve host|curl: \(\d+\)|Downloading .* failed|wget: unable|Name or service not known|"
         r"Connection timed out)", "the installer could not be downloaded — check the internet connection."),
        (r"(is (known to be )?broken|does not work (on|with)|not supported on|win64 prefix|64-bit WINEPREFIX.*not "
         r"supported)", "winetricks says this package does not work in this kind of prefix — usually it is not "
         "needed under Proton, which brings its own replacement."),
        (r"(returned empty string|wineserver: .*(failed|error)|Could not find wine|wine: could not load)",
         "Wine could not run in the game's prefix — it may be damaged: Diagnose → “Rebuild the prefix”."),
        (r"(No space left on device|ENOSPC)", "the drive is full."),
        (r"(cancel|aborted by user|User aborted)", "the installer was closed before it finished."),
    ]

    TRIAL_WINETRICKS_SECONDS = 900

    def _winetricks_into(self, entry_id: int, verbs: list[str], prefix: Path, log_path: Path,
                         proton_name: str | None = None) -> int:
        """Install winetricks verbs into a test copy of the prefix (with the Proton the test uses)."""
        import re as _re
        if not all(_re.fullmatch(r"[a-z0-9_]+", v) for v in verbs):
            return 2
        e = self.get(entry_id)
        proton = self._proton_named(e, proton_name)
        wt = shutil.which("winetricks")
        if proton is None or not wt:
            return 127
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} winetricks {' '.join(verbs)} (test copy)\n")
        proc = launcher.spawn(launcher.LaunchPlan([wt, "-q", *verbs], str(prefix), winetricks_env(proton, prefix),
                                                  "winetricks"), log_path)
        try:
            return proc.wait(timeout=self.TRIAL_WINETRICKS_SECONDS)
        except subprocess.TimeoutExpired:
            launcher.terminate(proc)
            return 124

    def _winetricks_failure(self, entry_id: int, verbs: str, log_path: Path | None = None) -> str:
        """Why the last winetricks run failed, in words (from its output in the game's log)."""
        import re as _re
        if log_path is not None:
            try:
                tail = log_path.read_bytes()[-512 * 1024:].decode("utf-8", "replace")
            except OSError:
                tail = ""
        else:
            tail = self._log_tail(entry_id, 512 * 1024)
        text = tail.rsplit(f" winetricks {verbs}", 1)[-1][-200000:]
        for rx, why in self._WT_REASONS:
            if _re.search(rx, text, _re.I):
                return why
        last = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith(("===", "fixme:"))][-1:]
        return ("winetricks stopped with an error" + (f": “{last[0][:160]}”" if last else "") + ".")

    def reset_prefix(self, entry_id: int) -> None:
        if self.is_running(entry_id):
            raise LibraryError("Stop the game first.")
        d = self.prefix_dir(entry_id)
        if d.exists():
            self.backup_saves(entry_id, "before-reset", quiet=True)
            if not is_within(d, self.paths.prefixes) or d.resolve() == self.paths.prefixes.resolve():
                raise LibraryError("Refusing to delete a folder outside the app's prefixes.")
            shutil.rmtree(d)

    # ------------------------------------------------- Steam overlay
    def _runner_shortcut(self, e: LibraryEntry) -> tuple[Path, str, int]:
        exe, _args = steamrun.runner()
        opts = steamrun.launch_options(e.id)
        return exe, opts, steam.shortcut_appid(f'"{exe}"', e.title)

    def talks_to_steam_itself(self, e: LibraryEntry) -> bool:
        """OnlineFix's fix uses the running Steam client itself. Started by Steam (or told a Steam game id) it
        switches to its "Steamworks activation", which changes Steam's own data — on 2026-10-11 that left Steam
        crashing on every start until it was fully reinstalled. Such games are only ever started by the app."""
        if e.source == "onlinefix":
            return True
        if not e.executable:
            return False
        exe = Path(e.executable)
        try:
            if suggest_dll_overrides(exe).onlinefix:
                return True
            root = Path(e.install_path) if e.install_path else exe.parent
            for folder in {exe.parent, root}:
                if folder.is_dir() and any((folder / n).exists() for n in ("OnlineFix.ini", "OnlineFix64.dll",
                                                                            "OnlineFix.dll")):
                    return True
        except OSError:
            pass
        return self.onlinefix_ids(e)[0] is not None       # deeper in the folder (Unreal's Steamworks folder)

    @staticmethod
    def onlinefix_app_id(e: LibraryEntry) -> int | None:
        """The Steam app OnlineFix's fix connects to Steam as (``FakeAppId`` in OnlineFix.ini, usually 480 =
        Spacewar). Steam shows the player as playing that app, so the overlay must use it as the game id too: the
        overlay without a game id is friends-list only, and a different id (the shortcut's) makes OnlineFix ask
        for its "Steamworks activation"."""
        return LibraryManager.onlinefix_ids(e)[0]

    @staticmethod
    def onlinefix_ids(e: LibraryEntry) -> tuple[int | None, int | None]:
        """(FakeAppId, RealAppId) from the game's OnlineFix.ini — next to the exe, or anywhere in the game folder
        (Unreal games keep it in Engine/Binaries/ThirdParty/Steamworks/…)."""
        import configparser
        if not e.executable:
            return None, None
        exe = Path(e.executable)
        cands = [exe.parent / "OnlineFix.ini", *(p / "OnlineFix.ini" for p in list(exe.parents)[:3])]
        if e.install_path:
            root = Path(e.install_path)
            cands.insert(1, root / "OnlineFix.ini")
            key = str(root)
            hit = _OF_INI_CACHE.get(key)
            if hit is None or time.monotonic() - hit[0] > 120:        # a game folder can hold 100 000 files
                try:
                    found = [p for p in root.glob("**/OnlineFix.ini") if ".ezgd-backup-" not in str(p)][:5]
                except OSError:
                    found = []
                hit = _OF_INI_CACHE[key] = (time.monotonic(), found)
            cands += hit[1]
        for ini in cands:
            try:
                if not ini.is_file():
                    continue
                cp = configparser.ConfigParser(strict=False, interpolation=None)
                cp.read_string(ini.read_text("utf-8", "replace"))
                fake = cp.get("Main", "FakeAppId", fallback="").strip()
                real = cp.get("Main", "RealAppId", fallback="").strip()
                if fake.isdigit() and int(fake) > 0:
                    return int(fake), (int(real) if real.isdigit() else None)
            except (OSError, configparser.Error, ValueError):
                continue
        return None, None

    def onlinefix_env(self, e: LibraryEntry) -> dict[str, str]:
        """Steam ids for an OnlineFix game whose fix runs as the genuine app (FakeAppId == RealAppId, e.g. a free
        Friend's Pass installed in Steam): exactly what Steam itself passes when it starts that game — without
        them the Steam API finds no app id and the game quits at once (Little Nightmares III, exit code 53)."""
        if not self.talks_to_steam_itself(e):
            return {}
        fake, real = self.onlinefix_ids(e)
        if fake and fake == real and fake != 480:
            return {"SteamAppId": str(fake), "SteamGameId": str(fake), "SteamOverlayGameId": str(fake)}
        return {}

    def steam_overlay_status(self, entry_id: int) -> dict:
        """Can Play go through Steam (so the Steam overlay works)? ``ready``: Steam already knows the shortcut."""
        e = self.get(entry_id)
        out = {"use": False, "ready": False, "steam_running": False, "reason": ""}
        try:                                       # OnlineFix's fix only works with the Steam client running
            out["needs_steam"] = IS_LINUX and self.talks_to_steam_itself(e) and steam.steam_command() is not None
            out["steam_running"] = steam.is_steam_running()
        except Exception:
            out["needs_steam"] = False
        if not self.settings.settings.launch_through_steam:
            out["reason"] = "switched off in Settings"
            return out
        if "no_steam_overlay" in e.compat_config.fixes:
            out["reason"] = "the Steam overlay is off for this game"
            return out
        root = self.steam_root()
        if root is None or steam.steam_command() is None:
            out["reason"] = "Steam is not installed"
            return out
        if not steam.steam_users(root):
            out["reason"] = "nobody has signed in to Steam on this computer yet"
            return out
        if not e.executable:
            out["reason"] = "no executable chosen"
            return out
        exe, opts, appid = self._runner_shortcut(e)
        out.update(use=True, ready=steam.has_shortcut(root, appid, exe, opts), steam_running=steam.is_steam_running(),
                   appid=appid)
        return out

    def start_steam(self, timeout: float = 90.0) -> bool:
        """Start the Steam client in the background (on the user's Play) and wait until it is up and signed in."""
        if steam.is_steam_running():
            return True
        cmd = steam.steam_command()
        if cmd is None:
            return False
        import subprocess
        subprocess.Popen(cmd + ["-silent"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if steam.is_steam_running():
                time.sleep(12)                 # signing in takes a few seconds after the process is there
                return True
            time.sleep(1)
        return False

    def prepare_steam_shortcuts(self, restart_steam: bool = False) -> dict:
        """Add a play-mode shortcut for every game that can be played (except OnlineFix games), for every user.

        Only ever on the user's request (Play → "Restart Steam and play"). Steam must not be running while
        shortcuts.vdf is written (it would overwrite it when it exits); with ``restart_steam`` it is closed first.
        Every write is verified and backed up (:func:`steam.write_shortcuts`), and after a restart a watch puts
        the old list back by itself if Steam does not stay up."""
        root = self.steam_root()
        if root is None:
            raise LibraryError("Steam installation not found (set its folder in Settings).")
        was_running = steam.is_steam_running()
        if was_running:
            if not restart_steam:
                raise LibraryError("Steam is running; it has to be closed to add the games to it.")
            if not steam.shutdown_steam():
                raise LibraryError("Steam did not close. Close it yourself (Steam → Exit), then press Play again.")
        users = steam.steam_users(root)
        files = {u: root / "userdata" / u / "config" / "shortcuts.vdf" for u in users}
        before = {u: (f.read_bytes() if f.exists() else None) for u, f in files.items()}
        added = 0
        try:
            for e in self.repo.all():
                if not e.executable:
                    continue
                exe, opts, appid = self._runner_shortcut(e)
                for user in users:
                    steam.add_shortcut(root, user, name=e.title, exe=exe, start_dir=exe.parent, launch_options=opts)
                if e.steam_shortcut_appid != appid:
                    e.steam_shortcut_appid = appid
                    self.repo.update(e)
                added += 1
        except steam.SteamFileError as exc:
            self._put_back(files, before)
            raise LibraryError(str(exc)) from exc
        log.info("Steam shortcuts written for %d game(s) (%d user(s)); Steam %s", added, len(users),
                 "was restarted" if was_running else "was not running")
        threading.Thread(target=self._watch_steam, args=(files, before), name="steam-safety", daemon=True).start()
        return {"games": added, "users": len(users), "restarted": was_running}

    @staticmethod
    def _put_back(files: dict, before: dict) -> None:
        """Restore Steam's shortcut lists exactly as they were (Steam closed)."""
        for user, f in files.items():
            old = before.get(user)
            try:
                if old is None:
                    f.unlink(missing_ok=True)
                else:
                    tmp = f.with_suffix(".ezgd-tmp")
                    tmp.write_bytes(old)
                    os.replace(tmp, f)
            except OSError:
                log.exception("could not restore %s", f)

    STEAM_START_SECONDS = 120          # Steam must come up within this time after our change …
    STEAM_STAY_SECONDS = 180           # … and keep running this long, or the change is undone

    def _watch_steam(self, files: dict, before: dict) -> None:
        """Safety net: if Steam does not start and stay up after the shortcuts were added, undo them."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.STEAM_START_SECONDS and not steam.is_steam_running():
            time.sleep(2)
        healthy = steam.is_steam_running()
        if healthy:
            down_since = None
            t1 = time.monotonic()
            while time.monotonic() - t1 < self.STEAM_STAY_SECONDS:
                time.sleep(3)
                if steam.is_steam_running():
                    down_since = None
                elif down_since is None:
                    down_since = time.monotonic()
                elif time.monotonic() - down_since > 25:      # gone (not just Steam's own self-update restart)
                    healthy = False
                    break
        if healthy:
            log.info("Steam is running normally with the added games")
            return
        log.warning("Steam did not stay up after adding games to it — putting its shortcut list back")
        deadline = time.monotonic() + 120
        while steam.is_steam_running() and time.monotonic() < deadline:
            time.sleep(2)                               # only touch its files while it is closed
        if steam.is_steam_running():
            self.emit("notify", ("Steam needs a check", "Steam did not run normally after the games were added. "
                                 "Close Steam, then use Settings → Remove the app's games from Steam."))
            return
        self._put_back(files, before)
        self.settings.set("launch_through_steam", False)
        self.emit("notify", ("Steam was put back", "Steam did not start normally after your games were added, so "
                             "the app restored Steam's game list exactly as it was and switched the full overlay "
                             "off. Steam can be started again."))

    def check_steam_shortcuts(self) -> str | None:
        """At start: take out shortcuts an older version wrote with an .exe as icon (they crash Steam's Library)."""
        root = self.steam_root()
        if root is None or not steam.risky_app_shortcuts(root):
            return None
        if steam.is_steam_running():
            return ("Steam has games added by an older version of this app that can make Steam's Library crash. "
                    "Use Settings → Remove the app's games from Steam (it closes Steam first).")
        try:
            n = steam.repair_risky_app_shortcuts(root)
        except steam.SteamFileError as exc:
            log.warning("could not repair Steam shortcuts: %s", exc)
            return None
        for e in self.repo.all():
            if e.steam_shortcut_appid:
                e.steam_shortcut_appid = None
                self.repo.update(e)
        log.warning("removed %d crash-prone Steam shortcut(s) written by an older version", n)
        return f"Removed {n} game shortcut(s) an older version of this app had added to Steam (they could crash Steam's Library)."

    def remove_steam_shortcuts(self, restart_steam: bool = False) -> int:
        """Undo: remove every game this app added to Steam (Steam closed; with ``restart_steam`` it is closed)."""
        root = self.steam_root()
        if root is None:
            return 0
        if steam.is_steam_running():
            if not restart_steam:
                raise LibraryError("Close Steam first (Steam → Exit) — it rewrites its game list when it exits.")
            if not steam.shutdown_steam():
                raise LibraryError("Steam did not close. Close it yourself (Steam → Exit) and try again.")
        try:
            n = steam.remove_app_shortcuts(root)
        except steam.SteamFileError as exc:
            raise LibraryError(str(exc)) from exc
        for e in self.repo.all():
            if e.steam_shortcut_appid:
                e.steam_shortcut_appid = None
                self.repo.update(e)
        log.info("removed %d app shortcut(s) from Steam", n)
        return n

    def launch_via_steam(self, entry_id: int) -> dict:
        """Play through the game's Steam shortcut: Steam starts play mode, so the overlay is in the game."""
        e = self.get(entry_id)
        if self.is_running(entry_id):
            raise LibraryError(f"{e.title} is already running.")
        st = self.steam_overlay_status(entry_id)
        if not st.get("ready"):
            raise LibraryError("Steam does not know this game yet (" + (st.get("reason") or "shortcut missing") + ").")
        e.executable_trusted = True                  # confirmed in the app just now
        self.repo.update(e)
        steam.open_steam_url(f"steam://rungameid/{steam.shortcut_rungameid(st['appid'])}")
        log.info("%s started through Steam (shortcut %s)", e.title, st["appid"])
        self.emit("game_state", {"id": e.id, "running": False, "preparing": True})
        return {"appid": st["appid"]}

    def add_to_steam(self, entry_id: int, user_id: str | None = None) -> int:
        e = self.get(entry_id)
        if not e.executable:
            raise LibraryError("Select the game's executable first.")
        root = self.steam_root()
        if root is None:
            raise LibraryError("Steam installation not found (set its folder in Settings).")
        users = steam.steam_users(root)
        user = user_id or (users[0] if len(users) == 1 else None)
        if user is None:
            raise LibraryError("Several Steam accounts were found; choose one." if users else
                               "No Steam user profile found; start Steam once and sign in.")
        # the shortcut starts the game through this app (play mode): the same Proton setup and fixes as Play,
        # and Windows games work without configuring a compatibility tool in Steam
        exe, opts, _ = self._runner_shortcut(e)
        try:
            appid = steam.add_shortcut(root, user, name=e.title, exe=exe, start_dir=exe.parent, launch_options=opts)
        except steam.SteamFileError as exc:
            raise LibraryError(str(exc)) from exc
        e.steam_shortcut_appid = appid
        self.repo.update(e)
        self.emit("library_changed", e.id)
        return appid

    # ----------------------------------------------------------- installers
    def _repack_for(self, e: LibraryEntry) -> repack.Repack | None:
        for root in (e.setup.get("root"), e.install_path, e.download_path):
            rep = repack.detect(root) if root else None
            if rep is not None:
                return rep
        return None

    def _is_installer_release(self, e: LibraryEntry, rep: repack.Repack) -> bool:
        """A setup.exe with data files next to a playable game is that game's own extra, not a repack."""
        if rep.kind != "inno" or e.source == "fitgirl":
            return True
        return not repack._has_game_exe(rep.root)

    def installer_info(self, entry_id: int) -> dict | None:
        """The game's installer state: pending (run setup.exe first), installing, installed (repack files left)."""
        e = self.get(entry_id)
        st = dict(e.setup or {})
        if entry_id in self._installers:              # running (or exited and about to be reaped)
            return {**st, "state": "installing"}
        if st.get("state") == "installing":          # the app was closed while the installer ran
            if _prefix_processes(self.prefix_dir(entry_id)):
                return {**st, "state": "installing", "detached": True}   # still running on its own: wait for it
            info = {"target": st.get("target", ""), "started": st.get("started", 0), "root": st.get("root", "")}
            self._installer_exited(entry_id, info, None, None)
            e = self.get(entry_id)
            st = dict(e.setup or {})
        if st.get("state") == "installed":
            root = Path(st.get("root") or "")
            left = bool(st.get("root")) and root.is_dir() and repack.detect(root) is not None
            return {**st, "repack_left": left, "repack_bytes": repack.detect(root).data_bytes if left else 0}
        rep = self._repack_for(e)
        if rep is None or not self._is_installer_release(e, rep):
            if st:
                e.setup = {}
                self.repo.update(e)
            return None
        target = repack.install_target(Path(self.settings.settings.install_dir), e.title, rep.root)
        info = {**rep.to_dict(), "state": "pending", "target": str(target), "last_result": st.get("last_result")}
        if st.get("state") != "pending" or st.get("root") != str(rep.root):
            e.setup = {"state": "pending", "root": str(rep.root), "kind": rep.kind,
                       **({"last_result": st["last_result"]} if st.get("last_result") else {})}
            self.repo.update(e)
            self.emit("library_changed", e.id)
        return info

    def verify_repack(self, entry_id: int) -> dict:
        e = self.get(entry_id)
        rep = self._repack_for(e)
        if rep is None:
            raise LibraryError("No repack files were found for this game.")
        return repack.verify_md5(rep)

    def run_installer(self, entry_id: int, confirmed: bool) -> dict:
        """Run the repack's setup.exe (only after the user confirmed it), preset to install into the app's folder."""
        e = self.get(entry_id)
        if not confirmed:
            raise LibraryError("Running the installer needs your confirmation first.")
        if self.is_running(entry_id):
            raise LibraryError(f"{e.title} is already running.")
        rep = self._repack_for(e)
        if rep is None:
            raise LibraryError("No installer was found for this game (setup.exe with its data files). If you moved "
                               "the files, use Change install folder… to point the app at them.")
        target = repack.install_target(Path(self.settings.settings.install_dir), e.title, rep.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        compat = CompatConfig.from_dict(e.compat)
        compat.fixes = []                           # the game's fixes are for the game, not its installer
        method = e.launch_method if e.launch_method is not LaunchMethod.STEAM else LaunchMethod.AUTO
        try:
            plan = self.launch_plan(entry_id, {"executable": str(rep.setup), "launch_args": "", "steam_app_id": None,
                                               "compat": compat.to_dict(), "launch_method": method})
        except launcher.LaunchError as exc:
            raise LibraryError(str(exc)) from exc
        plan.argv = plan.argv + [f"/DIR={repack.windows_path(target)}"]
        if "PRESSURE_VESSEL_FILESYSTEMS_RW" in plan.env:     # the container must see the destination too
            plan.env["PRESSURE_VESSEL_FILESYSTEMS_RW"] += ":" + str(target.parent)
        plan.description = f"installer ({rep.label}) · " + plan.description
        log_path = self.paths.logs / f"game-{e.id}.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {plan.description}\n"
                     f"{launcher.display_command(plan)}\n")
        proc = launcher.spawn(plan, log_path)
        started = time.time()
        self._processes[e.id] = (proc, time.monotonic(), plan, log_path)
        self._installers[e.id] = {"target": str(target), "started": started, "root": str(rep.root)}
        e.setup = {"state": "installing", "root": str(rep.root), "kind": rep.kind, "target": str(target),
                   "started": started}
        self.repo.update(e)
        log.info("installer for %s started: %s → %s", e.title, rep.setup, target)
        self.emit("game_state", {"id": e.id, "running": True, "installing": True})
        self.emit("library_changed", e.id)
        return {"target": str(target), "setup": str(rep.setup)}

    def _installer_exited(self, entry_id: int, info: dict, code: int | None, log_path: Path | None) -> None:
        e = self.repo.get(entry_id)
        if e is None:
            return
        prefix = self.prefix_dir(entry_id)
        pfx = next((p for p in (prefix / "pfx", prefix / "wine", prefix) if (p / "system.reg").exists()), None)
        root = Path(info.get("root") or ".")
        found = repack.find_installed(Path(info.get("target") or root), pfx, float(info.get("started") or 0),
                                      e.title, exclude=root)
        if log_path is not None:
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n=== installer exited with code {code}; installed game: {found or 'not found'}\n")
            except OSError:
                pass
        st = {"root": str(root), "kind": (e.setup or {}).get("kind"), "exit_code": code}
        if found is not None:
            e.install_path = str(found)
            hint = None
            if e.game_id:
                g = self.games.get(e.game_id)
                hint = g.extra.get("launch_exe_hint") if g else None
            best = next((c for c in rank(find_executables(found), found, e.title, hint) if c.kind == "game"), None)
            if best is not None:
                e.executable = str(best.path)
                e.executable_trusted = False           # Play still asks once before running it
            e.status = LibraryStatus.INSTALLED if e.executable else LibraryStatus.EXTRACTED
            e.setup = {**st, "state": "installed", "installed_to": str(found), "installed_at": time.time()}
            e.notes = None
            self.repo.update(e)
            log.info("installer for %s finished: game in %s, executable %s", e.title, found, e.executable)
            self.emit("notify", ("Game installed", f"{e.title} is installed — " +
                                 ("press Play." if e.executable else "choose its executable in Library.")))
        else:
            why = ("The installer was closed before it finished, or it installed somewhere the app could not find. "
                   "Run it again, or use Change install folder… to point the app at the installed game.")
            e.setup = {**st, "state": "pending", "last_result": why}
            self.repo.update(e)
            self.emit("notify", ("Installer closed", f"{e.title}: no installed game was found."))
        self.emit("game_state", {"id": entry_id, "running": False, "installing": False,
                                 "installed": found is not None})
        self.emit("library_changed", entry_id)

    def delete_repack(self, entry_id: int) -> int:
        """Delete the repack files after the game is installed; returns the bytes freed."""
        from ezgd.core.fsutil import dir_size
        from ezgd.installation.volumes import volume_of
        e = self.get(entry_id)
        st = e.setup or {}
        if st.get("state") != "installed" or not st.get("root"):
            raise LibraryError("Install the game first — the repack files are needed until then.")
        if self.is_running(entry_id):
            raise LibraryError("Stop the game first.")
        root = Path(st["root"])
        installed = Path(e.install_path or "")
        targets = [root] if root.exists() else []
        if e.download_path and Path(e.download_path).is_file():
            dp = Path(e.download_path)
            v = volume_of(dp)
            targets += [p for p in dp.parent.iterdir() if v is not None and (pv := volume_of(p)) is not None
                        and pv.set_key == v.set_key] if v is not None else [dp]
        freed = 0
        for t in targets:
            if installed and (t.resolve() == installed.resolve() or is_within(installed, t)):
                raise LibraryError("The game was installed inside the repack folder, so it cannot be deleted.")
            if not self.safe_to_delete(t):
                raise LibraryError(f"Refusing to delete {t}: it is outside the app's download and installation "
                                   "folders. Delete it yourself if you are sure.")
        for t in targets:
            size = dir_size(t)
            if t.is_dir():
                shutil.rmtree(t)
            elif t.exists():
                t.unlink()
            freed += size
        if e.download_path and not Path(e.download_path).exists():
            e.download_path = None
        e.setup = {**st, "root": None, "repack_deleted": True}
        self.repo.update(e)
        self.emit("library_changed", e.id)
        return freed

    # --------------------------------------------------------------- removal
    def safe_to_delete(self, path: Path) -> bool:
        """Only folders below the configured download/install roots may be deleted by the app."""
        s = self.settings.settings
        roots = [Path(s.install_dir), Path(s.download_dir), self.paths.prefixes]
        rp = Path(os.path.realpath(path))
        for r in roots:
            rr = Path(os.path.realpath(r))
            if rp != rr and is_within(rp, rr):
                return True
        return False

    def remove(self, entry_id: int, delete_files: bool = False) -> list[str]:
        e = self.get(entry_id)
        if self.is_running(entry_id):
            raise LibraryError("Stop the game before removing it.")
        deleted: list[str] = []
        if delete_files:
            for p in {e.install_path, e.download_path} - {None}:
                path = Path(p)
                if not path.exists():
                    continue
                if not self.safe_to_delete(path):
                    raise LibraryError(f"Refusing to delete {path}: it is outside the app's download and "
                                       f"installation folders. Delete it manually if you are sure.")
            for p in {e.install_path, e.download_path} - {None}:
                path = Path(p)
                if path.is_dir():
                    shutil.rmtree(path)
                    deleted.append(str(path))
                elif path.exists():
                    path.unlink()
                    deleted.append(str(path))
        self.repo.delete(e.id)
        self.emit("library_changed", e.id)
        return deleted
