"""Render the vector app icon to PNG and ICO files for packaging."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from ezgd.ui.icons import app_icon  # noqa: E402

out = Path(__file__).resolve().parent / "build-assets"
out.mkdir(exist_ok=True)
app = QGuiApplication([])
icon = app_icon()
icon.pixmap(256, 256).save(str(out / "ezgd.png"))
if not icon.pixmap(256, 256).toImage().save(str(out / "ezgd.ico"), "ICO"):
    print("warning: ICO writer unavailable; Windows build will use the default icon")
print(f"icons written to {out}")
