"""Extraction safety and robustness with synthetic archives."""

import io
import os
import shutil
import stat
import tarfile
import threading
import zipfile
from pathlib import Path

import py7zr
import pytest

from ezgd.installation import archives as ar
from ezgd.installation.pipeline import choose_archives


def make_zip(path: Path, entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name, target in (symlinks or {}).items():
            zi = zipfile.ZipInfo(name)
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(zi, target)
    return path


def make_tar(path: Path, entries: dict[str, bytes], links: dict[str, str] | None = None) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for name, target in (links or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            tf.addfile(ti)
    return path


def test_zip_extracts_into_destination(tmp_path):
    a = make_zip(tmp_path / "g.zip", {"Game/game.exe": b"MZ" + b"x" * 5000, "Game/data/a.pak": b"d" * 9000})
    progress = []
    res = ar.extract(a, tmp_path / "out", progress=lambda f, i: progress.append(f))
    assert (tmp_path / "out" / "Game" / "game.exe").read_bytes().startswith(b"MZ")
    assert res.files == 2 and res.bytes == 14002
    assert progress and progress[-1] == 1.0
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(ar.STAGING_PREFIX)]


@pytest.mark.parametrize("evil", ["../evil.txt", "a/../../evil.txt", "/abs/evil.txt", "C:/win/evil.txt",
                                  "..\\..\\evil.txt", "ok/file:stream"])
def test_zip_traversal_rejected_before_writing(tmp_path, evil):
    a = make_zip(tmp_path / "evil.zip", {"good.txt": b"ok", evil: b"pwned"})
    dest = tmp_path / "out"
    with pytest.raises(ar.UnsafeArchiveError):
        ar.extract(a, dest)
    assert not (tmp_path / "evil.txt").exists()
    assert not (dest / "good.txt").exists()          # nothing written at all


def test_zip_symlinks_are_skipped(tmp_path):
    a = make_zip(tmp_path / "l.zip", {"real.txt": b"ok"}, symlinks={"link": "/etc/passwd"})
    res = ar.extract(a, tmp_path / "out")
    assert (tmp_path / "out" / "real.txt").exists()
    assert not os.path.lexists(tmp_path / "out" / "link")
    assert res.skipped == ["link"]


def test_tar_links_and_traversal(tmp_path):
    t = make_tar(tmp_path / "a.tar.gz", {"dir/f.txt": b"x"}, links={"dir/l": "../../../etc/passwd"})
    res = ar.extract(t, tmp_path / "out")
    assert (tmp_path / "out" / "dir" / "f.txt").exists()
    assert not os.path.lexists(tmp_path / "out" / "dir" / "l") and res.skipped
    bad = make_tar(tmp_path / "b.tar.gz", {"../x.txt": b"x"})
    with pytest.raises(ar.UnsafeArchiveError):
        ar.extract(bad, tmp_path / "out2")


def test_7z_plain_and_password(tmp_path):
    src = tmp_path / "src"
    (src / "Game").mkdir(parents=True)
    (src / "Game" / "game.exe").write_bytes(b"MZ" * 1000)
    plain = tmp_path / "p.7z"
    with py7zr.SevenZipFile(plain, "w") as z:
        z.writeall(src / "Game", "Game")
    ar.extract(plain, tmp_path / "o1")
    assert (tmp_path / "o1" / "Game" / "game.exe").exists()
    enc = tmp_path / "e.7z"
    with py7zr.SevenZipFile(enc, "w", password="online-fix.me") as z:
        z.writeall(src / "Game", "Game")
    with pytest.raises(ar.ArchiveError):
        ar.extract(enc, tmp_path / "o2")              # no password
    ar.extract(enc, tmp_path / "o3", password="online-fix.me")
    assert (tmp_path / "o3" / "Game" / "game.exe").read_bytes() == b"MZ" * 1000


def test_truncated_archives_reported_as_corrupt(tmp_path):
    a = make_zip(tmp_path / "t.zip", {"f.bin": os.urandom(200_000)})
    data = a.read_bytes()
    a.write_bytes(data[: len(data) // 2])
    with pytest.raises(ar.CorruptArchiveError):
        ar.extract(a, tmp_path / "out")
    t = make_tar(tmp_path / "t.tar.gz", {"f.bin": os.urandom(200_000)})
    t.write_bytes(t.read_bytes()[:5000])
    with pytest.raises(ar.CorruptArchiveError):
        ar.extract(t, tmp_path / "out2")


def test_crc_corruption_detected(tmp_path):
    a = make_zip(tmp_path / "c.zip", {"f.txt": b"A" * 100_000})
    with zipfile.ZipFile(a) as zf:
        off = zf.getinfo("f.txt").header_offset
    raw = bytearray(a.read_bytes())
    for i in range(off + 60, off + 80):        # flip compressed payload bytes
        raw[i] ^= 0xFF
    a.write_bytes(bytes(raw))
    with pytest.raises((ar.CorruptArchiveError, ar.ArchiveError)):
        ar.extract(a, tmp_path / "out")
    assert not (tmp_path / "out" / "f.txt").exists()


def test_failure_leaves_destination_untouched(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("user data")
    a = make_zip(tmp_path / "x.zip", {"f.bin": os.urandom(100_000), "g.bin": os.urandom(100_000)})
    raw = a.read_bytes()
    a.write_bytes(raw[: int(len(raw) * 0.7)])
    with pytest.raises(ar.ArchiveError):
        ar.extract(a, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]


def test_cancellation(tmp_path):
    a = make_zip(tmp_path / "big.zip", {f"f{i}.bin": os.urandom(300_000) for i in range(20)})
    cancel = threading.Event()

    def progress(frac, item):       # first report arrives after the first chunk
        if frac > 0:
            cancel.set()
    with pytest.raises(ar.ExtractionCancelled):
        ar.extract(a, tmp_path / "out", progress=progress, cancel=cancel)
    assert not list((tmp_path / "out").iterdir())


def test_disk_space_checked(tmp_path, monkeypatch):
    a = make_zip(tmp_path / "s.zip", {"f.bin": b"\0" * 1_000_000})
    import ezgd.core.fsutil as fsutil
    monkeypatch.setattr(fsutil, "free_space", lambda p: 10)
    with pytest.raises(OSError, match="Not enough free space"):
        ar.extract(a, tmp_path / "out")


def test_nested_archives_reported_not_extracted(tmp_path):
    inner = make_zip(tmp_path / "inner.zip", {"deep.txt": b"x"})
    outer = make_zip(tmp_path / "outer.zip", {"Game/inner.zip": inner.read_bytes(), "Game/a.txt": b"a"})
    res = ar.extract(outer, tmp_path / "out")
    assert res.nested_archives == [str(Path("Game") / "inner.zip")]
    assert not (tmp_path / "out" / "Game" / "deep.txt").exists()


def test_detection_and_volumes(tmp_path):
    z = make_zip(tmp_path / "noext", {"a": b"b"})
    assert ar.detect_format(z) is ar.ArchiveFormat.ZIP
    (tmp_path / "x.txt").write_text("hello")
    assert ar.detect_format(tmp_path / "x.txt") is None
    assert ar.is_secondary_volume(Path("game.part2.rar")) and not ar.is_secondary_volume(Path("game.part1.rar"))
    assert ar.is_secondary_volume(Path("game.part02.rar")) and not ar.is_secondary_volume(Path("game.part01.rar"))
    assert ar.is_secondary_volume(Path("game.r00")) and ar.is_secondary_volume(Path("game.7z.002"))
    assert not ar.is_secondary_volume(Path("game.rar"))
    d = tmp_path / "dl"
    d.mkdir()
    for n in ("g.part1.rar", "g.part2.rar"):
        (d / n).write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\0" * 100)
    (d / "notes.txt").write_text("x")
    assert [p.name for p in ar.find_archives(d)] == ["g.part1.rar"]


def test_choose_archives_skips_fix_repair():
    main, left = choose_archives([Path("PEAK/PEAK.v2.6.b-OFME.rar"),
                                  Path("PEAK/Fix Repair/PEAK_Fix_Repair_Steam_V4_Generic.rar")])
    assert [p.name for p in main] == ["PEAK.v2.6.b-OFME.rar"] and len(left) == 1
    only, rest = choose_archives([Path("X/Fix Repair/fix.rar")])
    assert len(only) == 1 and not rest


def test_member_path_validation(tmp_path):
    assert ar.safe_member_path(tmp_path, "a/b/c.txt") == tmp_path / "a" / "b" / "c.txt"
    for bad in ["", "a\x00b", "../x", "/x", "C:\\x", "\\\\server\\share\\x", "a/../../x"]:
        with pytest.raises(ar.UnsafeArchiveError):
            ar.safe_member_path(tmp_path, bad)


def test_staging_cleanup(tmp_path):
    (tmp_path / "Game" / f"{ar.STAGING_PREFIX}123-456").mkdir(parents=True)
    (tmp_path / "Game" / "real").mkdir()
    assert ar.cleanup_stale_staging(tmp_path) == 1
    assert (tmp_path / "Game" / "real").exists()


UNRAR_LT = """
UNRAR 7.30 beta 1 freeware      Copyright (c) 1993-2026 Alexander Roshal

Archive: test.rar
Details: RAR 5

        Name: Game/game.exe
        Type: File
        Size: 1234
 Packed size: 1000

        Name: Game
        Type: Directory

        Name: Game/link
        Type: Symbolic link
      Target: /etc/passwd
"""


def test_unrar_listing_parser():
    members = ar._parse_unrar_technical(UNRAR_LT)
    assert [(m.name, m.size, m.is_dir, m.is_link) for m in members] == [
        ("Game/game.exe", 1234, False, False), ("Game", None, True, False), ("Game/link", None, False, True)]


@pytest.mark.skipif(not shutil.which("rar"), reason="'rar' (to create test archives) is not installed")
def test_real_rar_with_password(tmp_path):
    import subprocess
    src = tmp_path / "Game"
    src.mkdir()
    (src / "g.exe").write_bytes(b"MZ" * 100)
    subprocess.run(["rar", "a", "-hp" + "pw", "-idq", str(tmp_path / "g.rar"), "Game"], cwd=tmp_path, check=True)
    ar.extract(tmp_path / "g.rar", tmp_path / "out", password="pw")
    assert (tmp_path / "out" / "Game" / "g.exe").exists()


def test_missing_rar_tool_message(tmp_path, monkeypatch):
    fake = tmp_path / "fake.rar"
    fake.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\0" * 64)
    monkeypatch.setattr(ar, "find_rar_tool", lambda explicit=None: None)
    with pytest.raises(ar.MissingToolError, match="UnRAR or 7-Zip"):
        ar.extract(fake, tmp_path / "out")


@pytest.mark.skipif(not shutil.which("unrar"), reason="unrar not installed")
def test_corrupt_rar_reported(tmp_path):
    fake = tmp_path / "bad.rar"
    fake.write_bytes(b"Rar!\x1a\x07\x01\x00" + os.urandom(500))
    with pytest.raises(ar.ArchiveError):
        ar.extract(fake, tmp_path / "out")


def test_zip_with_zstandard_compression(tmp_path):
    """AnkerGames' zips (e.g. Absolver) use Zstandard (method 93): "That compression method is not supported"."""
    from backports.zstd import zipfile as zstd_zip
    from ezgd.installation import archives as ar
    archive = tmp_path / "Absolver-AnkerGames.zip"
    with zstd_zip.ZipFile(archive, "w", compression=zstd_zip.ZIP_ZSTANDARD) as zf:
        zf.writestr("Absolver/Absolver.exe", b"MZ" + b"\0" * 50_000)
        zf.writestr("Absolver/readme.txt", b"hello " * 1000)
    assert {i.compress_type for i in zstd_zip.ZipFile(archive).infolist()} == {93}
    res = ar.extract(archive, tmp_path / "out")
    assert res.files == 2 and (tmp_path / "out" / "Absolver" / "readme.txt").read_bytes() == b"hello " * 1000
