"""Settings → System check: tick the missing tools to install (umu-launcher is marked highly recommended)."""

import sys

import pytest

from test_ui import ui  # noqa: F401 — the shared main-window fixture

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux package managers")


def _fake_tools(monkeypatch, installed: set[str]):
    from ezgd.platform import deps
    monkeypatch.setattr(deps, "detect_family", lambda os_release=None: "arch")
    monkeypatch.setattr(deps, "_proton_capable", lambda: False)
    monkeypatch.setattr(deps, "tool_detail", lambda key: "")
    fake = [deps.Tool(t.key, t.name, t.purpose, (lambda k=t.key: k in installed), t.packages, t.importance, t.note)
            for t in deps.LINUX_TOOLS]
    monkeypatch.setattr(deps, "LINUX_TOOLS", fake)
    monkeypatch.setattr(deps, "available_packages", lambda fam, pkgs: (list(pkgs), []))
    return deps


def test_umu_is_highly_recommended_and_a_plan_takes_only_the_chosen_tools(monkeypatch):
    deps = _fake_tools(monkeypatch, {"rar"})
    assert next(t for t in deps.LINUX_TOOLS if t.key == "umu").importance == "highly_recommended"
    assert deps.plan(only=["umu", "mangohud"]).install == ["umu-launcher", "mangohud", "lib32-mangohud"]
    assert deps.plan(only=["steam"]).install == ["steam"]          # Steam alone, when umu is not chosen


def test_the_dialog_installs_what_is_ticked(ui, qtbot, monkeypatch):
    from ezgd.ui.dialogs.system import SystemCheckDialog
    win, core, _ = ui
    _fake_tools(monkeypatch, {"rar", "winetricks"})
    asked = []

    async def install(only=None):
        asked.append(only)
        return 0
    monkeypatch.setattr(core, "system_install", install)
    dlg = SystemCheckDialog(win.actions, win)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: bool(dlg.checks) and "Will install" in dlg.plan_label.text(), timeout=5000)
    assert set(dlg.checks) == {"umu", "steam", "gamemode", "mangohud", "gamescope"}       # only missing ones
    assert dlg.selected == {"umu", "gamemode", "mangohud", "gamescope"}                    # optional Steam not
    texts = [w.text() for w in dlg.findChildren(type(dlg.plan_label))]
    assert "★ Highly recommended" in texts
    for key in ("gamemode", "mangohud", "gamescope"):
        dlg.checks[key].setChecked(False)
    qtbot.waitUntil(lambda: "gamescope" not in dlg.plan_label.text(), timeout=5000)
    assert "Will install: umu-launcher" in dlg.plan_label.text() and dlg.install_btn.isEnabled()
    dlg.checks["umu"].setChecked(False)
    assert "Nothing selected" in dlg.plan_label.text() and not dlg.install_btn.isEnabled()
    dlg.checks["umu"].setChecked(True)
    qtbot.waitUntil(lambda: dlg.install_btn.isEnabled(), timeout=5000)
    dlg._install()
    qtbot.waitUntil(lambda: asked == [["umu"]], timeout=5000)
    dlg._live.stop()
    dlg.reject()


def test_the_rar_check_says_when_unrar_itself_is_not_installed(monkeypatch):
    """A ✔ next to "UnRAR / 7-Zip" read as "unrar is installed" when only the app's own 7-Zip was there."""
    from ezgd.installation import archives
    from ezgd.platform import deps
    monkeypatch.setattr(archives, "bundled_7zip", lambda: "/app/tools/7zzs")
    monkeypatch.setattr(archives, "find_rar_tool", lambda explicit=None: ("7z", "/app/tools/7zzs"))
    assert deps.tool_detail("rar").startswith("UnRAR is not installed — the 7-Zip that comes with the app")
    assert next(t for t in deps.LINUX_TOOLS if t.key == "rar").name == "RAR extractor"
    monkeypatch.setattr(archives, "find_rar_tool", lambda explicit=None: ("unrar", "/usr/bin/unrar"))
    assert deps.tool_detail("rar") == "UnRAR at /usr/bin/unrar"
