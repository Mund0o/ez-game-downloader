"""Starting games through Steam, so the Steam overlay works (plus Steam Input, screenshots and Steam's playtime).

Steam only puts its overlay into programs it starts itself. Every game in the Library therefore gets a non-Steam
shortcut in Steam that runs *this app* in play mode (``ezgd --play <id>``): Steam starts it with the overlay
loaded, and play mode starts the game exactly as the Play button does (the same Proton, runtime, fixes and launch
options), waits for it, writes the game log and records the playtime. The Play button then simply asks Steam to
start that shortcut (``steam://rungameid/…``).

Steam reads ``shortcuts.vdf`` only when it starts and writes it back when it exits, so shortcuts are only written
while Steam is closed (the app restarts Steam for that, after asking). While a game runs, play mode keeps a small
file in ``running/`` so the app shows it as running and can stop it.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

HOME: str | None = None             # portable mode folder (--home), passed on to play mode


def runner() -> tuple[Path, list[str]]:
    """(program, arguments before ``--play``) that start this app from a Steam shortcut."""
    exe = Path(sys.executable).resolve()
    args: list[str] = [] if getattr(sys, "frozen", False) else ["-m", "ezgd"]
    if HOME:
        args += ["--home", HOME]
    return exe, args


def launch_options(entry_id: int) -> str:
    import shlex
    _exe, args = runner()
    return " ".join(shlex.quote(a) for a in args + ["--play", str(entry_id)])


def marker(running_dir: Path, entry_id: int) -> Path:
    return running_dir / f"game-{entry_id}.json"


def read_marker(running_dir: Path, entry_id: int) -> dict | None:
    """The play-mode session of this game if one is running (stale files are removed)."""
    p = marker(running_dir, entry_id)
    try:
        info = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    pid = int(info.get("runner") or 0)
    try:
        if pid <= 0:
            raise ProcessLookupError
        os.kill(pid, 0)
    except ProcessLookupError:
        p.unlink(missing_ok=True)               # play mode is gone (crashed or killed)
        return None
    except PermissionError:
        pass
    if os.path.exists(f"/proc/{pid}/cmdline"):  # a reused process number (e.g. after a reboot) is not play mode
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
        except OSError:
            cmd = b""
        if b"--play" not in cmd and pid != os.getpid():
            p.unlink(missing_ok=True)
            return None
    return info


def sessions(running_dir: Path) -> dict[int, dict]:
    out = {}
    for p in running_dir.glob("game-*.json") if running_dir.is_dir() else []:
        try:
            eid = int(p.stem.split("-", 1)[1])
        except ValueError:
            continue
        info = read_marker(running_dir, eid)
        if info is not None:
            out[eid] = info
    return out


def stop(running_dir: Path, entry_id: int) -> bool:
    info = read_marker(running_dir, entry_id)
    if info is None:
        return False
    # the game and everything it started (wineserver …); play mode then records the session and exits itself
    pid = int(info.get("game") or 0) or int(info.get("runner") or 0)
    try:
        if os.name != "nt" and pid == int(info.get("game") or 0):
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return True


def _confirm(title: str, exe: str) -> bool:
    """Play mode was started from Steam for a file never confirmed in the app: ask once."""
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication(sys.argv[:1])
    _ = app
    r = QMessageBox.question(None, "EZ Game Downloader",
                             f"Start “{title}”?\n\n{exe}\n\nThis file has not been started from EZ Game Downloader "
                             "before. Only run games you trust.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    return r == QMessageBox.Yes


def play(paths, entry_id: int) -> int:
    """Play mode (run by Steam): start the game like the Play button, wait for it, record the session."""
    from ezgd.core.settings import SettingsStore
    from ezgd.database.db import Database
    from ezgd.database.repositories import GameRepository, LibraryRepository
    from ezgd.library.manager import LibraryError, LibraryManager
    from ezgd.library.models import LaunchMethod
    from ezgd.platform import launcher

    db = Database(paths.database, backup_dir=paths.backups).open()
    try:
        settings = SettingsStore(db)
        lib = LibraryManager(LibraryRepository(db), GameRepository(db), settings, paths, lambda *_a: None)
        try:
            e = lib.get(entry_id)
        except LibraryError:
            log.error("play mode: library entry %s not found", entry_id)
            return 2
        if not e.executable:
            log.error("play mode: %s has no executable chosen", e.title)
            return 2
        if not e.executable_trusted and not _confirm(e.title, e.executable):
            return 1
        switched = lib.ensure_prefix_for(e)           # never a prefix another kind of Proton made (see prefixguard)
        if switched:
            log.info("play mode: %s", switched)
        overrides = {"launch_method": LaunchMethod.AUTO} if e.launch_method is LaunchMethod.STEAM else None
        try:
            plan = lib.launch_plan(entry_id, overrides)
        except (LibraryError, launcher.LaunchError) as exc:
            log.error("play mode: %s cannot start: %s", e.title, exc)
            return 2
        # Steam sets SteamAppId for shortcuts; the Steam API reads it before anything else and a wrong id breaks
        # games (see proton_command). The overlay uses SteamOverlayGameId / SteamGameId, which stay.
        if not e.compat_config.report_app_id:
            os.environ.pop("SteamAppId", None)
        if lib.talks_to_steam_itself(e):
            # OnlineFix asks for a "Steamworks activation" when it sees the shortcut's game id (2026-10-11), but
            # the overlay without any game id is only the friends list (2026-10-12). OnlineFix connects to Steam
            # as its FakeAppId (Spacewar, 480), so the game id becomes that: what OnlineFix expects, and a real
            # app for the overlay. SteamAppId stays unset — the Steam API reads it before OnlineFix's own id.
            for k in ("SteamAppId", "STEAM_COMPAT_APP_ID"):
                os.environ.pop(k, None)
            fake = lib.onlinefix_app_id(e)
            genuine = lib.onlinefix_env(e)          # the fix runs as the real Steam app: Steam's own ids
            if genuine:
                os.environ.update(genuine)
                log.info("play mode: %s uses OnlineFix as Steam app %s — Steam ids set like Steam does", e.title,
                         genuine["SteamAppId"])
            elif fake:
                os.environ["SteamGameId"] = os.environ["SteamOverlayGameId"] = str(fake)
                log.info("play mode: %s uses OnlineFix — Steam game id set to its app %s (full overlay)", e.title, fake)
            else:
                for k in ("SteamGameId", "SteamClientLaunch", "SteamEnv"):
                    os.environ.pop(k, None)
                log.info("play mode: %s uses OnlineFix (no FakeAppId found) — Steam's game ids hidden", e.title)
        log_path = paths.logs / f"game-{e.id}.log"
        overlay = "loaded" if "gameoverlayrenderer" in os.environ.get("LD_PRELOAD", "") or os.name == "nt" else \
            "not loaded (not started by Steam?)"
        with open(log_path, "a", encoding="utf-8") as fh:
            desc = f"{plan.description} · through Steam (overlay {overlay})"
            plan.description = desc
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {desc}\n"
                     f"{lib.setup_snapshot(e, plan)}\n{launcher.display_command(plan)}\n")
        if plan.pre:
            launcher.run_pre_launch(plan, log_path)
        proc = launcher.spawn(plan, log_path)
        started = time.monotonic()
        running = paths.data / "running"
        running.mkdir(parents=True, exist_ok=True)
        mk = marker(running, e.id)
        mk.write_text(json.dumps({"runner": os.getpid(), "game": proc.pid, "started": time.time()}))

        def forward(signum, _frame):                 # Steam's Stop / logout: stop the game too
            launcher.terminate(proc)
        import threading
        if os.name != "nt" and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, forward)
            signal.signal(signal.SIGHUP, forward)
        try:
            e = lib.get(entry_id)
            e.executable_trusted = True
            e.play_count += 1
            e.last_played_at = time.time()
            lib.repo.update(e)
            code = proc.wait()
        finally:
            mk.unlink(missing_ok=True)
        played = int(time.monotonic() - started)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== exited with code {code} after {played}s\n")
        if plan.post:
            launcher.run_post_exit(plan, code, log_path)
        e = lib.get(entry_id)
        e.playtime_seconds += played
        e.last_played_at = time.time()
        p = lib._proton_for(e)
        lib.remember_working_proton(e, p.name if p and plan.description.startswith(p.name) else None, played)
        lib.repo.update(e)
        log.info("play mode: %s exited with code %s after %ss", e.title, code, played)
        return int(code or 0)
    finally:
        db.close()
