"""Release integrity: missing/damaged files are found from the archive's own file list and repaired selectively."""

import io
import os
import zipfile

import py7zr
import pytest

from ezgd.installation import archives as ar
from ezgd.installation import integrity

FILES = {"Game/Game.exe": b"MZ" + b"\0" * 5000, "Game/data/level1.pak": b"L" * 20000,
         "Game/data/level2.pak": b"K" * 30000, "Game/settings.ini": b"[video]\nw=1920\n"}


def make(tmp_path, kind):
    a = tmp_path / f"Game.{kind}"
    if kind == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, d in FILES.items():
                z.writestr(n, d)
        a.write_bytes(buf.getvalue())
    else:
        with py7zr.SevenZipFile(a, "w") as z:
            for n, d in FILES.items():
                z.writestr(d, n)
    dest = tmp_path / "games" / "G"
    ar.extract(a, dest)
    return a, dest


@pytest.mark.parametrize("kind", ["zip", "7z"])
def test_find_and_repair(tmp_path, kind):
    a, dest = make(tmp_path, kind)
    (dest / "Game" / "data" / "level1.pak").unlink()                       # missing
    (dest / "Game" / "data" / "level2.pak").write_bytes(b"K" * 100)        # cut off
    (dest / "Game" / "settings.ini").write_text("[video]\nw=2560\nh=1440\n")  # changed by the player: fine
    (dest / "Game" / "Game.exe").touch()
    rec = [{"archive": str(a), "dest": str(dest)}]
    rep = integrity.check(rec)
    assert rep.checked == 4 and {(i.rel, i.kind) for i in rep.issues} == {
        ("Game/data/level1.pak", "missing"), ("Game/data/level2.pak", "damaged")}
    before = os.stat(dest / "Game" / "Game.exe").st_mtime_ns
    r = integrity.repair(rep)
    assert r["repaired"] == 2 and not r["failed"]
    assert (dest / "Game" / "data" / "level1.pak").read_bytes() == FILES["Game/data/level1.pak"]
    assert (dest / "Game" / "data" / "level2.pak").read_bytes() == FILES["Game/data/level2.pak"]
    assert "2560" in (dest / "Game" / "settings.ini").read_text()          # not overwritten
    assert os.stat(dest / "Game" / "Game.exe").st_mtime_ns == before       # untouched
    assert integrity.check(rec).issues == []


def test_later_archive_wins_and_renamed_files_and_missing_archive(tmp_path):
    a, dest = make(tmp_path, "zip")
    fix = tmp_path / "Fix.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Game/Game.exe", b"MZ fixed exe")                         # the fix replaces the exe
    fix.write_bytes(buf.getvalue())
    ar.extract(fix, dest)
    (dest / "Game" / "data" / "level1.pak").rename(dest / "Game" / "data" / "level1.pak.old")   # release script
    rep = integrity.check([{"archive": str(a), "dest": str(dest)}, {"archive": str(fix), "dest": str(dest)}])
    assert rep.issues == []
    gone = integrity.check([{"archive": str(tmp_path / "deleted.rar"), "dest": str(dest)}])
    assert gone.unverifiable == ["deleted.rar"] and gone.checked == 0
