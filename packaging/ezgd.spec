# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for EZ Game Downloader (one-folder build).
#   Linux:   packaging/build_linux.sh
#   Windows: packaging\build_windows.ps1
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent
ASSETS = Path(SPECPATH) / "build-assets"
WINDOWS = sys.platform == "win32"

datas = [(str(ROOT / "ezgd" / "resources" / "certs"), "ezgd/resources/certs"),
         (str(ROOT / "ezgd" / "resources" / "adblock"), "ezgd/resources/adblock"),
         (str(ROOT / "ezgd" / "resources" / "couch"), "ezgd/resources/couch"),       # the couch intro video
         (str(ROOT / "config.sample.toml"), ".")]
datas += collect_data_files("certifi")
binaries = collect_dynamic_libs("libtorrent")

# Only QtCore/QtGui/QtWidgets (+ QtMultimedia for the couch intro) are used; keep heavy Qt modules out.
excludes = ["tkinter", "PySide6.QtWebEngineQuick",   # QtWebEngineCore/Widgets = the in-app browser
            "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
            "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization",
            "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth",
            "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort"]

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["libtorrent", "py7zr", "rarfile", "lxml.etree", "lxml._elementpath", "bs4",
                   "backports.zstd", "backports.zstd.zipfile", "adblock", "PySide6.QtMultimedia"],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = ASSETS / ("ezgd.ico" if WINDOWS else "ezgd.png")
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ezgd",
    console=False,
    icon=str(icon) if icon.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="EZGameDownloader")
