"""Game update detection: which release is installed, and is the source offering a newer one?

The installed release is recorded when the game is extracted (from the archive name, e.g.
``How.to.Fish.v1.0.10-OFME.rar`` → ``1.0.10``, else the version the game page listed then). The check re-reads the
game's page (rate-limited, cached; never in the background for sources that ask tools not to crawl them) and
compares versions numerically. Only a clearly newer version is reported as an update; a different version that
cannot be ordered ("Build 17" vs "v1.2") is reported as "a different version" so the user decides.
"""

from __future__ import annotations

import re
import time
from typing import Any

# v1.0.10 · 0.111.0 · 1.2.3a · Build 7660664 · b18234 · 2026.10.01
_IN_NAME = re.compile(r"(?:^|[\s._\-(\[])(?:v|ver\.?|version\s*)(\d+(?:[._]\d+)*[a-z]?)(?=$|[\s._\-)\]+])", re.I)
_BARE = re.compile(r"(?:^|[\s._\-(\[])(\d+(?:\.\d+){1,4}[a-z]?)(?=$|[\s_\-)\]+]|\.(?:rar|zip|7z|\d{3})\b)", re.I)
_BUILD = re.compile(r"\b(?:build|b)[\s._#]*(\d{3,})\b", re.I)
_FIX = re.compile(r"(fix[\s._-]*repair|fix files only|_fix_|hot[\s._-]?fix)", re.I)

UPDATE, DIFFERENT, CURRENT, UNKNOWN = "update", "different", "current", "unknown"


def parse_version(text: str | None) -> str | None:
    """The version in a release/archive name or a page's version field ("v1.0.10", "Build 123", "1.2.3")."""
    if not text:
        return None
    t = str(text).strip()
    name = re.sub(r"\.(rar|zip|7z|tar|gz|xz|\d{3})$", "", t, flags=re.I)
    name = re.sub(r"\.part\d+$", "", name, flags=re.I)
    m = _IN_NAME.search(name) or _IN_NAME.search(" " + name)
    if m:
        return m.group(1).replace("_", ".")
    m = _BUILD.search(name)
    if m:
        return "build " + m.group(1)
    m = _BARE.search(" " + name)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d+[a-z]?", t, re.I):              # a page's bare build number
        return t
    return None


def _key(v: str) -> tuple[str, tuple] | None:
    v = v.strip().lower()
    if v.startswith("build "):
        return ("build", (int(v.split()[1]),))
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)([a-z]?)", v)
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    while len(nums) > 1 and nums[-1] == 0:                # 1.2.0 == 1.2
        nums = nums[:-1]
    return ("ver", nums + ((ord(m.group(2)) - 96,) if m.group(2) else ()))


def compare(a: str | None, b: str | None) -> int | None:
    """-1 / 0 / 1 when ``a`` is older / the same / newer than ``b``; None when they cannot be ordered."""
    if not a or not b:
        return None
    ka, kb = _key(a), _key(b)
    if ka is None or kb is None:
        return 0 if a.strip().lower() == b.strip().lower() else None
    if ka[0] != kb[0]:
        return None
    return (ka[1] > kb[1]) - (ka[1] < kb[1])


def is_fix_archive(name: str) -> bool:
    return bool(_FIX.search(name or ""))


def installed_release(archive_names: list[str], page_version: str | None) -> dict[str, Any]:
    """What was installed, from the extracted archives (newest last) or the page's version at that time."""
    for name in reversed(archive_names):
        if is_fix_archive(name):
            continue
        v = parse_version(name)
        if v:
            return {"version": v, "from": "archive", "archive": name}
    v = parse_version(page_version) if page_version else None
    if v or page_version:
        return {"version": v or str(page_version), "from": "page"}
    return {}


def status(installed: str | None, latest: str | None) -> str:
    if not installed or not latest:
        return UNKNOWN
    c = compare(latest, installed)
    if c is None:
        return DIFFERENT
    return UPDATE if c > 0 else CURRENT


def describe(release: dict) -> str:
    st = release.get("status")
    have, new = release.get("version"), release.get("latest")
    if st == UPDATE:
        return f"Version {new} is available (you have {have})."
    if st == DIFFERENT:
        return f"The page now lists version {new} (you have {have})."
    if st == CURRENT:
        return f"Up to date ({have})."
    return "Not checked yet." if not release.get("checked_at") else "The source does not say which version it has."


def checked(release: dict, latest: str | None, page_updated: float | None) -> dict:
    r = dict(release)
    r["checked_at"] = time.time()
    if latest:
        r["latest"] = latest
    if page_updated:
        r["page_updated_at"] = page_updated
    r["status"] = status(r.get("version"), r.get("latest"))
    return r
