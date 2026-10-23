"""External tools the app can use, how to detect them, and how to install them.

One list drives both ``install.sh`` (via ``ezgd --setup-system``) and the
in-app *System check*, so the installer and the app never disagree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ezgd.platform import IS_LINUX, IS_WINDOWS


@dataclass
class Tool:
    key: str
    name: str
    purpose: str
    detect: Callable[[], bool]
    packages: dict[str, list[str]]           # distro family → package names
    importance: str = "recommended"          # required | highly_recommended | recommended | optional
    note: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolStatus:
    key: str
    name: str
    purpose: str
    installed: bool
    importance: str
    packages: list[str]
    note: str | None
    detail: str = ""              # what was found (program and where), or why it does not count
    bundled: bool = False         # not installed — the copy that comes with the app does the job (7-Zip for RAR)


def _which(*names: str) -> Callable[[], bool]:
    return lambda: any(shutil.which(n) for n in names)


def _rar_tool() -> bool:
    """A RAR extractor installed on the system. The app's own 7-Zip does not count — with it, the check showed a ✔
    on a computer that had no UnRAR at all (it extracts, but nothing was installed); see ``_rar_bundled``."""
    from ezgd.installation.archives import bundled_7zip, find_rar_tool
    tool = find_rar_tool()
    # (bsdtar cannot open password-protected RAR archives)
    return tool is not None and tool[0] != "bsdtar" and tool[1] != bundled_7zip()


def _rar_bundled() -> bool:
    """RAR archives are extracted by the 7-Zip that comes with the app (nothing installed on the system)."""
    from ezgd.installation.archives import bundled_7zip, find_rar_tool
    tool = find_rar_tool()
    return tool is not None and tool[1] == bundled_7zip()


def _steam() -> bool:
    from ezgd.platform.steam import find_steam_root
    return bool(find_steam_root() or shutil.which("steam"))


def _proton_capable() -> bool:
    """Something that can run Windows games: Steam's Proton or umu-launcher."""
    from ezgd.platform.steam import find_proton_versions, find_steam_root
    return bool(shutil.which("umu-run") or find_proton_versions(find_steam_root()))


LINUX_TOOLS: list[Tool] = [
    Tool("rar", "RAR extractor", "Extracts RAR archives — both sources ship games as RAR", _rar_tool,
         {"arch": ["unrar", "7zip"], "debian": ["unrar", "7zip"], "fedora": ["7zip", "unrar"],
          "suse": ["7zip", "unrar"]}, "required",
         {"debian": "unrar is in the non-free (Debian) / multiverse (Ubuntu) component; 7zip covers RAR too.",
          "fedora": "unrar comes from RPM Fusion; 7zip handles RAR archives as well."}),
    Tool("umu", "umu-launcher", "Runs Windows games with Proton, even without Steam (downloads GE-Proton and "
                                "the Steam Runtime on first use)", _which("umu-run"),
         {"arch": ["umu-launcher"], "fedora": ["umu-launcher"], "debian": [], "suse": []}, "highly_recommended",
         {"debian": "Not packaged by Debian/Ubuntu — install Steam instead, or get the .deb from "
                    "github.com/Open-Wine-Components/umu-launcher/releases.",
          "suse": "Not packaged — install Steam instead."}),
    Tool("steam", "Steam", "Provides Proton builds and the Steam Linux Runtime (optional if umu-launcher is "
                           "installed)", _steam,
         {"arch": ["steam"], "debian": ["steam-installer"], "fedora": ["steam"], "suse": ["steam"]}, "optional",
         {"arch": "Needs the [multilib] repository.",
          "debian": "Needs i386 architecture and contrib/multiverse.",
          "fedora": "Comes from RPM Fusion (nonfree)."}),
    Tool("winetricks", "Winetricks", "Installs Windows runtimes (VC++, .NET, fonts) into a game's prefix",
         _which("winetricks"),
         {"arch": ["winetricks", "zenity"], "debian": ["winetricks", "zenity"],
          "fedora": ["winetricks", "zenity"], "suse": ["winetricks", "zenity"]}),
    Tool("gamemode", "GameMode", "CPU/GPU performance mode — launch option `gamemoderun %command%`",
         _which("gamemoderun"),
         {"arch": ["gamemode", "lib32-gamemode"], "debian": ["gamemode"], "fedora": ["gamemode"],
          "suse": ["gamemode"]}),
    Tool("mangohud", "MangoHud", "FPS / performance overlay — launch option `mangohud %command%`",
         _which("mangohud"),
         {"arch": ["mangohud", "lib32-mangohud"], "debian": ["mangohud"], "fedora": ["mangohud"],
          "suse": ["mangohud"]}),
    Tool("gamescope", "gamescope", "Valve's gaming compositor (resolution scaling, FSR) — "
                                   "`gamescope -- %command%`", _which("gamescope"),
         {"arch": ["gamescope"], "debian": ["gamescope"], "fedora": ["gamescope"], "suse": ["gamescope"]}),
]


def detect_family(os_release: str | None = None) -> str | None:
    """arch | debian | fedora | suse (from /etc/os-release ID / ID_LIKE)."""
    if os_release is None:
        try:
            os_release = Path("/etc/os-release").read_text()
        except OSError:
            return None
    ids: list[str] = []
    for line in os_release.splitlines():
        k, _, v = line.partition("=")
        if k in ("ID", "ID_LIKE"):
            ids += v.strip().strip('"').lower().split()
    for fam, names in (("arch", ("arch", "cachyos", "manjaro", "endeavouros", "garuda")),
                       ("debian", ("debian", "ubuntu", "linuxmint", "pop")),
                       ("fedora", ("fedora", "rhel", "nobara", "bazzite")),
                       ("suse", ("suse", "opensuse", "opensuse-tumbleweed"))):
        if any(i in names for i in ids):
            return fam
    return None


def tool_detail(key: str) -> str:
    """What the check actually found, so the System check says *which* program it uses."""
    if key == "rar":
        from ezgd.installation.archives import bundled_7zip, find_rar_tool
        tool = find_rar_tool()
        if tool is None:
            return "No RAR tool found."
        kind, path = tool
        if kind == "bsdtar":
            return f"Only bsdtar was found ({path}) — it cannot open password-protected RAR archives (OnlineFix)."
        if path == bundled_7zip():                # (shown without a ✔: nothing is installed on the system)
            return ("UnRAR is not installed — the 7-Zip that comes with the app extracts RAR archives instead "
                    "(password-protected ones too), so it is not needed.")
        return f"{'UnRAR' if kind == 'unrar' else '7-Zip'} at {path}"
    if key == "steam":
        from ezgd.platform.steam import find_steam_root
        root = find_steam_root()
        return f"Steam at {root}" if root else (f"steam at {shutil.which('steam')}" if shutil.which("steam") else "")
    prog = {"umu": "umu-run", "winetricks": "winetricks", "gamemode": "gamemoderun", "mangohud": "mangohud",
            "gamescope": "gamescope"}.get(key)
    path = shutil.which(prog) if prog else None
    return f"{prog} at {path}" if path else ""


def status(family: str | None = None) -> list[ToolStatus]:
    if not IS_LINUX:
        return windows_status()
    family = family or detect_family()
    out = []
    for t in LINUX_TOOLS:
        try:
            ok = t.detect()
        except Exception:
            ok = False
        try:
            detail = tool_detail(t.key)
        except Exception:
            detail = ""
        try:
            bundled = not ok and t.key == "rar" and _rar_bundled()
        except Exception:
            bundled = False
        out.append(ToolStatus(t.key, t.name, t.purpose, ok, t.importance,
                              t.packages.get(family or "", []), t.note.get(family or ""), detail, bundled))
    return out


def windows_status() -> list[ToolStatus]:
    ok = bundled = False
    if IS_WINDOWS:
        ok, bundled = _rar_tool(), _rar_bundled()
    return [ToolStatus("rar", "7-Zip", "Extracts RAR archives — both sources ship games as RAR", ok, "required",
                       ["7zip.7zip"], "Installed with winget.", bundled=bundled)]


def multilib_enabled(pacman_conf: str | None = None) -> bool:
    if pacman_conf is None:
        try:
            pacman_conf = Path("/etc/pacman.conf").read_text()
        except OSError:
            return False
    return any(line.strip() == "[multilib]" for line in pacman_conf.splitlines())


def available_packages(family: str, packages: list[str]) -> tuple[list[str], list[str]]:
    """Split packages into (installable from the configured repositories, unavailable)."""
    ok, missing = [], []
    for p in packages:
        if family == "arch":
            if p.startswith("lib32-") or p == "steam":
                if not multilib_enabled():
                    missing.append(p)
                    continue
            cmd = ["pacman", "-Si", p]
        elif family == "debian":
            cmd = ["apt-cache", "show", p]
        elif family == "fedora":
            cmd = ["dnf", "-q", "info", p]
        elif family == "suse":
            cmd = ["zypper", "--non-interactive", "info", p]
        else:
            missing.append(p)
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            found = r.returncode == 0 and (family != "suse" or "not found" not in r.stdout.lower())
        except (OSError, subprocess.SubprocessError):
            found = False
        (ok if found else missing).append(p)
    return ok, missing


def install_command(family: str, packages: list[str]) -> list[str]:
    if family == "arch":
        return ["pacman", "-S", "--needed", "--noconfirm", *packages]
    if family == "debian":
        return ["apt-get", "install", "-y", *packages]
    if family == "fedora":
        return ["dnf", "install", "-y", *packages]
    if family == "suse":
        return ["zypper", "--non-interactive", "install", *packages]
    raise ValueError(f"unsupported distribution family {family}")


@dataclass
class InstallPlan:
    family: str | None
    install: list[str]
    unavailable: list[str]
    notes: list[str]
    command: list[str] | None

    @property
    def shell(self) -> str:
        return " ".join(self.command) if self.command else ""


def plan(include_optional: bool = True, only: list[str] | None = None) -> InstallPlan:
    family = detect_family()
    if family is None:
        return InstallPlan(None, [], [], ["Your Linux distribution was not recognised; install the tools listed "
                                         "in the System check with your package manager."], None)
    wanted: list[str] = []
    notes: list[str] = []
    statuses = status(family)
    umu = next(s for s in statuses if s.key == "umu")
    umu_planned = not umu.installed and bool(umu.packages) and (not only or "umu" in only)
    for st in statuses:
        if st.installed or (only and st.key not in only):
            continue
        if st.bundled and not only:
            continue                         # the app's own copy does the job: installed only when ticked
        if st.importance == "optional" and not include_optional:
            continue
        if st.key == "steam" and (umu_planned or _proton_capable()):
            continue                         # Proton is covered by umu-launcher or an existing Proton
        wanted += [p for p in st.packages if p not in wanted]
        if st.note:
            notes.append(f"{st.name}: {st.note}")
        if not st.packages:
            notes.append(f"{st.name}: no package for this distribution. {st.note or ''}".strip())
    ok, missing = available_packages(family, wanted) if wanted else ([], [])
    return InstallPlan(family, ok, missing, notes, install_command(family, ok) if ok else None)


def elevated(command: list[str], gui: bool) -> list[str]:
    """Prefix a command with pkexec (graphical password prompt) or sudo (terminal)."""
    if os.geteuid() == 0:
        return command
    if gui and shutil.which("pkexec"):
        return ["pkexec", *command]
    if shutil.which("sudo"):
        return ["sudo", *command]
    raise RuntimeError("Neither pkexec nor sudo is available to install packages.")


def run_setup_cli(assume_yes: bool = False) -> int:
    """Interactive terminal setup used by install.sh (`ezgd --setup-system`)."""
    print("EZ Game Downloader — system check\n")
    if not IS_LINUX:
        print("Run packaging\\install_windows.ps1 on Windows.")
        return 0
    family = detect_family()
    for st in status(family):
        mark = "✔" if st.installed else "○" if st.bundled else ("✘" if st.importance == "required" else "–")
        star = " (highly recommended)" if st.importance == "highly_recommended" and not st.installed else ""
        print(f"  {mark} {st.name:<15} {st.purpose}{star}")
        if st.key == "rar" and st.detail:
            print(f"    {'':<15} {st.detail}")
    p = plan()
    print()
    for n in p.notes:
        print("  note:", n)
    if p.unavailable:
        print("  not available in your repositories:", ", ".join(p.unavailable))
    if not p.command:
        print("Everything the app can use is installed." if not p.unavailable else
              "Nothing more can be installed automatically.")
        return 0
    print(f"\nWill run:  sudo {p.shell}\n")
    if not assume_yes:
        try:
            answer = input("Install these packages now? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            print("Skipped. You can install them later from Settings → System check.")
            return 0
    if p.family == "debian":
        subprocess.run(elevated(["apt-get", "update"], gui=False))
    rc = subprocess.run(elevated(p.command, gui=False)).returncode
    print("\nDone." if rc == 0 else f"\nThe package manager exited with code {rc}.")
    return rc


if __name__ == "__main__":
    sys.exit(run_setup_cli("--yes" in sys.argv))
