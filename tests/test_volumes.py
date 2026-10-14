"""Multi-part archives: recognising parts, deciding completeness, extracting split sets, one library entry."""

import io
import zipfile

import py7zr
import pytest

from ezgd.installation import archives as ar
from ezgd.installation.volumes import expected_parts, set_status, volume_info, volume_of


def test_part_names_are_recognised():
    v = volume_info("No.Mans.Sky.v7.6.0.0-OFME.part3.rar")
    assert v.index == 3 and v.first_name == "No.Mans.Sky.v7.6.0.0-OFME.part1.rar"
    w = volume_info("Farming.Simulator.25.v1.24.0.0-OFME.part07.rar")
    assert w.index == 7 and w.first_name.endswith(".part01.rar")
    assert volume_info("Game.7z.002").first_name == "Game.7z.001"
    assert volume_info("Game.zip.001").index == 1
    assert volume_info("Game.r00").index == 2 and volume_info("Game.r00").first_name == "Game.rar"
    assert v.set_key == volume_info("No.Mans.Sky.v7.6.0.0-OFME.part1.rar").set_key
    assert v.set_key != volume_info("Other.part1.rar").set_key
    for single in ("DEADBOLT.v1.0.3-OFME.rar", "Game.zip", "Game.7z", "Silver.Pines-SteamRIP.com.rar"):
        assert volume_info(single) is None


def test_completeness(tmp_path):
    key = volume_info("G.part1.rar").set_key
    (tmp_path / "G.part1.rar").write_bytes(b"x" * 100)
    (tmp_path / "G.part2.rar").write_bytes(b"x" * 100)
    (tmp_path / "G.part3.rar.part").write_bytes(b"x" * 10)        # still downloading
    st = set_status(tmp_path, key, expected=3)
    assert not st.complete and st.missing == [3] and st.summary == "2 of 3 parts downloaded"
    (tmp_path / "G.part3.rar").write_bytes(b"x" * 40)
    assert set_status(tmp_path, key, expected=3).complete
    # without a known count: contiguous and the last part is smaller than the first
    assert set_status(tmp_path, key).complete
    (tmp_path / "G.part3.rar").write_bytes(b"x" * 100)               # same size: there may be more
    assert not set_status(tmp_path, key).complete
    names = [f"G.part{i}.rar" for i in (1, 2, 3, 4)] + ["G_Fix_Repair.rar"]
    assert expected_parts(names, key) == 4
    old = tmp_path / "old"
    old.mkdir()
    for n in ("H.rar", "H.r00", "H.r01"):
        (old / n).write_bytes(b"y" * (30 if n == "H.r01" else 100))
    assert volume_of(old / "H.rar").index == 1
    assert set_status(old, volume_info("H.r00").set_key).complete


def _split(data: bytes, base, n: int) -> list:
    size = -(-len(data) // n)
    out = []
    for i in range(n):
        p = base.parent / f"{base.name}.{i + 1:03d}"
        p.write_bytes(data[i * size:(i + 1) * size])
        out.append(p)
    return out


@pytest.mark.parametrize("kind", ["7z", "zip"])
def test_split_archives_extract_from_the_first_part(tmp_path, kind):
    payload = {"Game/Game.exe": b"MZ" + bytes(range(256)) * 400, "Game/data.pak": b"D" * 60_000}
    whole = tmp_path / f"whole.{kind}"
    if kind == "7z":
        with py7zr.SevenZipFile(whole, "w") as z:
            for name, data in payload.items():
                z.writestr(data, name)
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for name, data in payload.items():
                z.writestr(name, data)
        whole.write_bytes(buf.getvalue())
    parts = _split(whole.read_bytes(), tmp_path / f"Game.{kind}", 3)
    whole.unlink()
    assert ar.detect_format(parts[0]) is not None and ar.find_archives(tmp_path) == [parts[0]]
    dest = tmp_path / "out"
    ar.extract(parts[0], dest)
    assert (dest / "Game" / "data.pak").read_bytes() == payload["Game/data.pak"]


async def test_parts_from_separate_downloads_become_one_game(tmp_path, app_paths):
    """In-app (e.g. Pixeldrain) parts finish one by one: one library entry, extracted once the last is in."""
    import httpx
    from ezgd.catalog.models import DownloadMethod, Game, MethodKind
    from ezgd.core.app_core import AppCore
    from ezgd.downloads.models import DownloadTask
    from ezgd.library.models import LibraryStatus
    core = AppCore(app_paths, transport=httpx.MockTransport(lambda r: httpx.Response(404)), enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        core.settings.set("install_dir", str(tmp_path / "games"))
        whole = tmp_path / "w.7z"
        with py7zr.SevenZipFile(whole, "w") as z:
            z.writestr(b"MZ" + b"\0" * 50_000, "Split Game/Game.exe")
        dl = tmp_path / "dl"
        dl.mkdir()
        parts = _split(whole.read_bytes(), dl / "Split.Game.7z", 3)
        names = [p.name for p in parts]
        g = Game("onlinefix", "9", "Split Game", "https://online-fix.me/games/x/9-split.html", download_methods=[
            DownloadMethod(MethodKind.DIRECT, f"Pixeldrain — {n}", f"https://pixeldrain.com/u/{i}", file_name=n,
                           supported=True) for i, n in enumerate(names)])
        core.games.save(g)
        staged = {p: p.read_bytes() for p in parts}
        for p in parts:
            p.unlink()
        for i, p in enumerate(parts):                         # parts arrive one at a time
            p.write_bytes(staged[p])
            task = DownloadTask(id=None, kind="http", uri=f"https://pixeldrain.com/u/{i}", title=g.title,
                                dest_dir=str(dl), filename=p.name,
                                game_id=g.id, source=g.source, auto_extract=True)
            entry = await core.post.on_download_completed(task, p)
            entries = [e for e in core.library.all() if e.game_id == g.id]
            assert len(entries) == 1                          # never one entry per part
            if i < 2:
                assert entries[0].status is LibraryStatus.DOWNLOADED
                assert f"{i + 1} of 3 parts downloaded" in entries[0].notes
        e = core.library.get(entry.id)
        assert e.status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED) and "all 3 parts downloaded" in e.notes
        assert (tmp_path / "games" / "Split Game" / "Split Game" / "Game.exe").exists()
        assert e.download_path == str(parts[0])
    finally:
        await core.shutdown()


async def test_browser_parts_imported_in_any_order(tmp_path, app_paths):
    import asyncio
    import httpx
    from ezgd.catalog.models import Game
    from ezgd.core.app_core import AppCore
    from ezgd.library.models import LibraryStatus
    core = AppCore(app_paths, transport=httpx.MockTransport(lambda r: httpx.Response(404)), enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        core.settings.set("install_dir", str(tmp_path / "games"))
        whole = tmp_path / "w.7z"
        with py7zr.SevenZipFile(whole, "w") as z:
            z.writestr(b"MZ" + b"\0" * 50_000, "B/Game.exe")
        dl = tmp_path / "Browser downloads"
        dl.mkdir()
        parts = _split(whole.read_bytes(), dl / "B.Game.7z", 3)       # last part is the smallest
        g = Game("steamrip", "5", "B Game", "https://steamrip.com/b/")   # page lists no file names
        core.games.save(g)
        staged = {p: p.read_bytes() for p in parts}
        for p in parts:
            p.unlink()
        results = []
        for p in (parts[2], parts[0], parts[1]):                   # any order
            p.write_bytes(staged[p])
            results.append(await core.import_downloaded_files(g.id, [str(p)]))
        assert [r["complete"] for r in results] == [False, False, True]
        assert len({r["entry_id"] for r in results}) == 1
        for _ in range(100):
            if core.library.get(results[0]["entry_id"]).status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED):
                break
            await asyncio.sleep(0.05)
        assert core.library.get(results[0]["entry_id"]).status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED)
        assert all(p.exists() for p in parts)                      # kept: the setting is off by default
        # with "delete the archive after extraction" on, every part goes once the set is extracted
        core.settings.set("delete_archive_after_extract", True)
        core.library.remove(results[0]["entry_id"])
        results = [await core.import_downloaded_files(g.id, [str(p) for p in parts])]
        for _ in range(100):
            if core.library.get(results[-1]["entry_id"]).status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED):
                break
            await asyncio.sleep(0.05)
        assert core.library.get(results[-1]["entry_id"]).status in (LibraryStatus.EXTRACTED, LibraryStatus.INSTALLED)
        assert not any(p.exists() for p in parts)
    finally:
        await core.shutdown()
