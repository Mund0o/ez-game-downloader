"""Application entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import warnings
from pathlib import Path

from ezgd import APP_DISPLAY_NAME, APP_NAME, __version__

log = logging.getLogger("ezgd")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ezgd", description=APP_DISPLAY_NAME)
    p.add_argument("--home", help="portable mode: keep all data, settings and cache in this folder")
    p.add_argument("--import-config", metavar="TOML", help="apply settings from a config.toml file")
    p.add_argument("--setup-system", action="store_true",
                   help="check for and install the external tools the app uses (unrar/7-Zip, umu-launcher, "
                        "gamemode, MangoHud, gamescope, winetricks), then exit")
    p.add_argument("--yes", action="store_true", help="with --setup-system: do not ask for confirmation")
    p.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)       # smoke testing
    p.add_argument("--page", default="home", help=argparse.SUPPRESS)
    p.add_argument("--quit-after", type=float, default=0, help=argparse.SUPPRESS)
    p.add_argument("--browser-check", metavar="URL", help=argparse.SUPPRESS)   # smoke test of the in-app browser
    p.add_argument("--open-dialog", metavar="KIND:ID", help=argparse.SUPPRESS)  # smoke test: diagnose:<library id>
    p.add_argument("--play", type=int, metavar="ID", help=argparse.SUPPRESS)   # play mode, started by Steam
    p.add_argument("--minimized", action="store_true",
                   help="start in the tray (or minimized), e.g. when started with the desktop session")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _handle_db_problems(core, QMessageBox) -> bool:
    """Open the database, guiding the user through unusual states. Returns False to quit."""
    from ezgd.database.db import SchemaTooNewError
    try:
        core.open_database()
    except SchemaTooNewError as exc:
        box = QMessageBox()
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(APP_DISPLAY_NAME)
        box.setText("The database was created by a newer version of EZ Game Downloader.")
        box.setInformativeText(f"{exc}\n\nUpdate the app, or start with a new empty database (the existing file is "
                               f"kept next to it, renamed).")
        fresh = box.addButton("Start with a new database", QMessageBox.DestructiveRole)
        box.addButton("Quit", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not fresh:
            return False
        db_path = core.paths.database
        target = db_path.with_name(f"{db_path.name}.newer-{time.strftime('%Y%m%d-%H%M%S')}")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                os.replace(src, Path(str(target) + suffix))
        core.open_database()
    if core.db.recovered_from:
        QMessageBox.warning(None, APP_DISPLAY_NAME,
                            "The database file was damaged and could not be opened. It was moved to:\n"
                            f"{core.db.recovered_from}\n\nA new database was created. You can restore an earlier "
                            "backup in Settings → Database.")
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.setup_system:
        from ezgd.platform.deps import run_setup_cli
        return run_setup_cli(assume_yes=args.yes)
    from ezgd.core.logging_setup import setup_logging
    from ezgd.library import steamrun
    from ezgd.platform.paths import resolve_paths
    paths = resolve_paths(args.home)
    paths.ensure()
    steamrun.HOME = str(Path(args.home).expanduser().resolve()) if args.home else None
    if args.play is not None:
        # started by a Steam shortcut: run one game (no window of our own), so Steam's overlay is in it
        import logging.handlers
        from ezgd.core.logging_setup import LOG_FORMAT, RedactingFormatter
        fh = logging.handlers.RotatingFileHandler(paths.logs / "steam-play.log", maxBytes=512 * 1024,
                                                  backupCount=2, encoding="utf-8")
        fh.setFormatter(RedactingFormatter(LOG_FORMAT))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(logging.INFO)
        log.info("play mode for library entry %s (started by Steam)", args.play)
        return steamrun.play(paths, args.play)
    from ezgd.core import instance
    if not instance.acquire(paths.data):
        # already running for this data folder: bring that window to the front instead of a second copy
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication(sys.argv[:1])
        if not instance.ask_running_copy_to_show(paths.data):
            QMessageBox.information(None, APP_DISPLAY_NAME, f"{APP_DISPLAY_NAME} is already running.")
        _ = app
        return 0
    log_file = setup_logging(paths.logs, console=sys.stderr is not None)
    from ezgd.core import crashreport
    previous_crash = crashreport.install(paths.logs)     # also reports a crash of the previous session
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    log.info("%s %s starting (data: %s)", APP_DISPLAY_NAME, __version__, paths.data)
    try:
        return _run(args, paths, log_file, previous_crash)
    finally:
        crashreport.mark_clean_exit()           # not reached when the process is killed by a crash
        instance.release()


def _run(args, paths, log_file, previous_crash) -> int:
    from ezgd.core import crashreport

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    try:                    # the in-app browser must be loaded before the QApplication exists
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        log.info("in-app browser (Qt WebEngine) not available")

    app = QApplication(sys.argv[:1])
    from ezgd.ui import gcguard
    gcguard.install(app)                  # garbage collection on the main thread only (see gcguard.py)
    from PySide6.QtCore import Qt as _Qt
    from ezgd.library import screenread   # the debugging assistant never photographs the app's own windows
    app.applicationStateChanged.connect(lambda st: screenread.set_app_focused(st == _Qt.ApplicationActive))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("ezgd")
    from ezgd.ui import theme
    theme.apply_theme(app)

    from ezgd.core.app_core import AppCore
    core = AppCore(paths)
    if not _handle_db_problems(core, QMessageBox):
        return 1
    first_run = core.db.query_one("SELECT COUNT(*) FROM settings")[0] == 0

    from ezgd.ui.bridge import Bridge, CoreThread
    thread = CoreThread()
    thread.start()
    bridge = Bridge(thread, core)
    try:
        bridge.run_sync(core.start(), timeout=60)
        if first_run and paths.config_file.exists():
            for w in bridge.run_sync(core.import_config(str(paths.config_file))):
                log.warning("config.toml: %s", w)
        if args.import_config:
            for w in bridge.run_sync(core.import_config(args.import_config)):
                log.warning("%s: %s", args.import_config, w)
        settings = bridge.run_sync(core.get_settings())
    except Exception:
        log.exception("start-up failed")
        from ezgd.ui.dialogs.crash import show_report
        show_report(crashreport.report_exception(*sys.exc_info(), where="while starting"))
        return 1
    theme.apply_theme(app, settings["theme_accent"], settings["theme_contrast"])

    from ezgd.ui.main_window import MainWindow
    win = MainWindow(bridge, paths, settings)
    if args.minimized and win.tray is not None:
        pass                                  # in the tray: the icon opens it
    elif args.minimized:
        win.showMinimized()
    else:
        win.show()
    from ezgd.core import instance

    def bring_to_front() -> None:
        win.showNormal() if win.isMinimized() or not win.isVisible() else None
        win.raise_()
        win.activateWindow()
    _instance_server = instance.listen(paths.data, bring_to_front, app)   # a second start shows this window
    from ezgd.ui.dialogs.crash import CrashNotifier
    notifier = CrashNotifier(lambda: win, app)
    crashreport.set_callback(notifier.submit)        # errors nothing handled → a window with a copyable code
    if previous_crash is not None and not (args.screenshot or args.quit_after):
        QTimer.singleShot(1500, lambda: notifier.submit(previous_crash))
    # everything loaded at start (modules, classes, the window) lives as long as the app: the full garbage
    # collection skips it from now on (200,000 objects: 33 ms → 2 ms, a dropped frame every 30 s in couch mode)
    QTimer.singleShot(4000, gcguard.freeze_startup)
    if args.page == "couch":
        QTimer.singleShot(1500, win.open_couch)               # smoke testing couch mode
    elif args.page != "home":
        win.navigate(args.page)
    if args.browser_check:
        from ezgd.ui.dialogs import browser
        if not browser.AVAILABLE:
            log.error("browser-check: Qt WebEngine is not available")
        else:
            from ezgd.catalog.models import Game
            logging.getLogger("ezgd.ui.dialogs.browser").setLevel(logging.DEBUG)   # list what gets blocked
            check = Game("check", "1", "Browser check", args.browser_check)
            dlg = browser.BrowserDownloadDialog(win.actions, check, args.browser_check, "check", win)
            dlg.show()
            dlg.view.loadFinished.connect(lambda ok: QTimer.singleShot(3000, lambda: log.info(
                "browser-check: loaded=%s title=%r url=%s blocked=%d popups=%d", ok, dlg.current.title(),
                dlg.current.url().toString(), browser.ADBLOCK.count(browser._site(browser._host(dlg.current.url()))),
                dlg._popups_blocked)))
    shown = []
    if args.open_dialog:
        kind, _, ident = args.open_dialog.partition(":")

        def open_it():
            if kind == "diagnose":
                from ezgd.ui.dialogs.diagnose import DiagnoseDialog
                e = bridge.run_sync(core.library_call("get", int(ident)))
                dlg = DiagnoseDialog(win.actions, e, win)
            else:
                return
            dlg.show()
            shown.append(dlg)
        QTimer.singleShot(1500, open_it)
    if args.screenshot or args.quit_after:
        def finish():
            if args.screenshot:
                (shown[-1] if shown else (win.couch or win)).grab().save(args.screenshot)
                log.info("screenshot saved to %s", args.screenshot)
            win.force_quit()
        QTimer.singleShot(int((args.quit_after or 8) * 1000), finish)

    if os.name != "nt":
        # SIGTERM (logout, `kill`, updates) → the normal quit path: downloads paused, resume data and backup saved
        import signal

        def on_term(signum, _frame):
            log.info("received signal %s, quitting", signum)
            QTimer.singleShot(0, win.force_quit)
        signal.signal(signal.SIGTERM, on_term)
        wake = QTimer(app)                # let Python run signal handlers while Qt's loop is idle
        wake.timeout.connect(lambda: None)
        wake.start(500)

    rc = app.exec()
    crashreport.set_callback(None)
    log.info("shutting down")
    try:
        from ezgd.ui.dialogs import browser
        browser.shutdown()
    except Exception:
        log.exception("in-app browser did not shut down cleanly")
    from ezgd.ui import artwork
    artwork.shutdown()                    # couch mode's artwork helper process
    try:
        bridge.run_sync(core.shutdown(), timeout=30)
    except Exception:
        log.exception("shutdown did not complete cleanly")
    thread.stop()
    return rc


def exit_now(rc: int) -> None:
    """End the process once the app has shut itself down.

    Everything that matters is saved by then (database closed, torrents' resume data written, logs flushed).
    Python's own interpreter teardown is skipped on purpose: PySide6 then destroys the remaining Qt objects in
    no particular order, and with Qt WebEngine loaded that can crash in QObject's destructor (2026-10-11: SIGBUS
    in PySide::destroyQCoreApplication after a normal quit, reported as a crash with a core dump)."""
    try:
        logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.flush()
    finally:
        os._exit(int(rc or 0))


def run() -> None:
    """Console/GUI entry point: run the app, then exit without the crash-prone teardown (in every case)."""
    import multiprocessing
    multiprocessing.freeze_support()              # the page-parsing helper process (packaged app: same program)
    try:
        rc = main()
    except SystemExit as exc:                     # argparse --help/--version, explicit exits
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:                         # reported (crash report / log) before leaving
        sys.excepthook(*sys.exc_info())
        rc = 1
    exit_now(rc)


if __name__ == "__main__":
    run()
