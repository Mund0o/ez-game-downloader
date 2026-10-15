"""The environment for the computer's own programs (kscreen-doctor, pactl, xrandr …) started from the app.

The packaged app puts its bundled libraries first on LD_LIBRARY_PATH. A system program started with that inherits
them — a Qt tool then loads the app's Qt and does not start at all ("Could not find the Qt platform plugin"). This
gives the original value back (PyInstaller keeps it in LD_LIBRARY_PATH_ORIG) or leaves the variable out."""

from __future__ import annotations

import os
import sys


def host_env(env: dict | None = None) -> dict:
    out = dict(os.environ if env is None else env)
    if not getattr(sys, "frozen", False):
        return out
    orig = out.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        out["LD_LIBRARY_PATH"] = orig
    else:
        out.pop("LD_LIBRARY_PATH", None)
    return out
