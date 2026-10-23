"""The launcher entry: the app can be pinned to the taskbar and keeps its own icon there while it is closed."""

import shlex
import sys

import pytest

from ezgd.platform import appentry, autostart

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="XDG desktop entries")

APP = "/home/u/EZ Game downloader/EZGameDownloader/ezgd"          # (the user's real folder has spaces)


def test_exec_quoting_follows_the_desktop_entry_spec():
    line = appentry.exec_line([APP, "--minimized"])
    assert line == '"/home/u/EZ Game downloader/EZGameDownloader/ezgd" --minimized'
    assert shlex.split(line) == [APP, "--minimized"]
    assert appentry.exec_quote('a"b$c`d\\e') == '"a\\"b\\$c\\`d\\\\e"'
    assert appentry.exec_quote("100%") == "100%%"
    assert "'" not in autostart.desktop_entry([APP, "--minimized"])     # single quotes are not in the spec


def test_the_launcher_is_written_with_the_windows_own_icon(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    exe = tmp_path / "ezgd"
    exe.write_text("#!/bin/sh\n")
    assert appentry.register(b"PNG-violet", "violet", [str(exe)])
    entry = (tmp_path / "applications" / "ezgd.desktop").read_text()
    icon = tmp_path / "icons" / "ezgd-violet.png"
    assert icon.read_bytes() == b"PNG-violet"
    assert f"Icon={icon}\n" in entry and "StartupWMClass=ezgd\n" in entry and f"Exec={exe}\n" in entry
    assert appentry.register(b"PNG-cyan", "cyan", [str(exe)])        # another accent: the same icon as the window
    assert not icon.exists() and (tmp_path / "icons" / "ezgd-cyan.png").read_bytes() == b"PNG-cyan"
    assert "ezgd-cyan.png" in (tmp_path / "applications" / "ezgd.desktop").read_text()


def test_a_launcher_from_an_installation_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    apps = tmp_path / "applications"
    apps.mkdir()
    installed = "[Desktop Entry]\nType=Application\nName=EZ Game Downloader\nExec=sh\nIcon=ezgd\n"
    (apps / "ezgd.desktop").write_text(installed)                   # install.sh's (its program exists)
    assert not appentry.register(b"PNG", "violet", ["/x/ezgd"])
    assert (apps / "ezgd.desktop").read_text() == installed
    (apps / "ezgd.desktop").write_text(installed.replace("Exec=sh", "Exec=/gone/ezgd"))   # its program is gone
    assert appentry.register(b"PNG", "violet", ["/x/ezgd"])
    assert appentry.MANAGED in (apps / "ezgd.desktop").read_text()


def test_a_portable_run_leaves_the_desktops_entries_alone(tmp_path, monkeypatch):
    # (a --home scratch copy once rewrote both: the real app then started that copy, hidden in the tray)
    from ezgd.library import steamrun
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    real = tmp_path / "cfg" / "autostart" / "ezgd.desktop"
    real.parent.mkdir(parents=True)
    real.write_text("[Desktop Entry]\nExec=/real/ezgd --minimized\n")
    monkeypatch.setattr(steamrun, "HOME", str(tmp_path / "scratch"))
    assert not appentry.register(b"PNG", "violet", [str(tmp_path / "ezgd")])
    assert not (tmp_path / "data" / "applications" / "ezgd.desktop").exists()
    autostart.apply(True)
    autostart.apply(False)
    assert real.read_text() == "[Desktop Entry]\nExec=/real/ezgd --minimized\n"
