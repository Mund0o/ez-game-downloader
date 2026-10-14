"""Real BitTorrent transfers between two local libtorrent sessions (no internet, synthetic data)."""

import time
from pathlib import Path

import libtorrent as lt
import pytest

from conftest import make_torrent, random_bytes
from ezgd.downloads.models import DownloadKind, DownloadStatus, DownloadTask, Verification
from ezgd.downloads.torrent_engine import TorrentEngine, TorrentError, inspect_torrent, magnet_info_hash

LOCAL = {"listen_port": 0, "listen_interface": "127.0.0.1", "listen_interface6": "", "enable_dht": False,
         "enable_lsd": False, "enable_upnp": False, "allow_local_peers": True}


def seeder(torrent: bytes, root: Path):
    ses = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                      "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(lt.bdecode(torrent))
    atp.save_path = str(root)
    atp.flags |= lt.torrent_flags.seed_mode
    h = ses.add_torrent(atp)
    deadline = time.time() + 5
    while ses.listen_port() == 0 and time.time() < deadline:
        time.sleep(0.05)
    return ses, h


@pytest.fixture
def swarm(tmp_path):
    files = {"Game/data.bin": random_bytes(600_000, seed=11), "Game/readme.txt": b"hello" * 100,
             "Game/Fix Repair/fix.bin": random_bytes(50_000, seed=12)}
    seed_root = tmp_path / "seed"
    tor = make_torrent(files, seed_root, "Pack")
    ses, h = seeder(tor, seed_root)
    yield tor, ses, files
    del ses


def make_task(tmp_path, tor, engine, **kw):
    meta = inspect_torrent(tor)
    path = engine.store_torrent_file(tor, meta.info_hash)
    return DownloadTask(id=kw.pop("id", 1), kind=DownloadKind.TORRENT, title="Pack", uri=str(path),
                        dest_dir=str(tmp_path / "dl"), torrent_file=str(path), **kw)


def drive(engine, task, until, seed_port, timeout=30.0):
    deadline = time.time() + timeout
    events = []
    last_offer = 0.0
    while time.time() < deadline:
        h = engine.handles.get(task.id)
        # no trackers/DHT in tests: keep offering the local seeder until a connection sticks
        if h is not None and h.is_valid() and h.status().num_peers == 0 and time.time() - last_offer > 1.5:
            h.connect_peer(("127.0.0.1", seed_port))
            last_offer = time.time()
        events += [e for e, t in engine.poll() if t is task]
        if until(task, events):
            return events
        time.sleep(0.1)
    raise AssertionError(f"timeout; status={task.status} detail={task.status_detail} err={task.error}")


def test_inspect_and_reject_invalid():
    with pytest.raises(TorrentError):
        inspect_torrent(b"not a torrent")
    with pytest.raises(TorrentError):
        inspect_torrent(b"")
    with pytest.raises(TorrentError):
        magnet_info_hash("magnet:?xt=urn:btih:zzz")
    ih = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
    assert magnet_info_hash(f"magnet:?xt=urn:btih:{ih}&dn=x") == ih


@pytest.mark.parametrize("name,path", [
    (b"x", [b"..", b"evil.txt"]), (b"x", [b"/etc", b"passwd"]), (b"x", [b"a", b"..", b"..", b"b"]),
    (b"../../up", None),
])
def test_hostile_torrent_paths_never_escape(name, path):
    """libtorrent sanitises names ('..' → '_'); inspect_torrent re-checks as a second layer."""
    info = {b"name": name, b"piece length": 16384, b"pieces": b"\x00" * 20}
    if path is None:
        info[b"length"] = 10
    else:
        info[b"files"] = [{b"length": 10, b"path": path}]
    try:
        meta = inspect_torrent(lt.bencode({b"info": info}))
    except TorrentError:
        return
    for f in meta.files:
        assert not Path(f.path).is_absolute() and ".." not in Path(f.path).parts


def test_full_download_from_local_swarm(tmp_path, swarm):
    tor, ses, files = swarm
    eng = TorrentEngine(LOCAL, tmp_path / "state")
    task = make_task(tmp_path, tor, eng)
    eng.add(task)
    events = drive(eng, task, lambda t, ev: t.status is DownloadStatus.COMPLETED, ses.listen_port())
    assert "completed" in events
    assert task.verification is Verification.PIECES
    for rel, data in files.items():
        assert (tmp_path / "dl" / "Pack" / rel).read_bytes() == data
    assert eng.content_path(task) == tmp_path / "dl" / "Pack"
    assert task.downloaded_bytes == task.total_bytes == sum(len(d) for d in files.values())
    eng.shutdown()


def test_selective_download(tmp_path, swarm):
    tor, ses, files = swarm
    meta = inspect_torrent(tor)
    keep = [f.index for f in meta.files if "Fix Repair" not in f.path]
    eng = TorrentEngine(LOCAL, tmp_path / "state")
    task = make_task(tmp_path, tor, eng, selected_files=keep)
    eng.add(task)
    drive(eng, task, lambda t, ev: t.status is DownloadStatus.COMPLETED, ses.listen_port())
    assert (tmp_path / "dl" / "Pack" / "Game" / "data.bin").exists()
    fix = tmp_path / "dl" / "Pack" / "Game" / "Fix Repair" / "fix.bin"
    assert not fix.exists() or fix.read_bytes() != files["Game/Fix Repair/fix.bin"]
    assert task.total_bytes == sum(len(d) for k, d in files.items() if "Fix Repair" not in k)
    eng.shutdown()


def test_pause_resume_and_resume_data_recovery(tmp_path, swarm):
    tor, ses, files = swarm
    eng = TorrentEngine(LOCAL, tmp_path / "state")
    task = make_task(tmp_path, tor, eng)
    eng.add(task, paused=True)
    eng.poll()
    assert task.status is DownloadStatus.PAUSED
    eng.resume(task)
    drive(eng, task, lambda t, ev: t.status is DownloadStatus.COMPLETED, ses.listen_port())
    eng.save_all_resume_data()
    assert task.resume_data
    eng.shutdown()

    # "restart": a new engine restores the torrent from resume data and recognises it as complete
    task2 = DownloadTask(id=1, kind=DownloadKind.TORRENT, title="Pack", uri=task.uri, dest_dir=task.dest_dir,
                         torrent_file=task.torrent_file, resume_data=task.resume_data)
    eng2 = TorrentEngine(LOCAL, tmp_path / "state")
    eng2.add(task2)
    deadline = time.time() + 15
    while time.time() < deadline and task2.status is not DownloadStatus.COMPLETED:
        eng2.poll()
        time.sleep(0.1)
    assert task2.status is DownloadStatus.COMPLETED
    eng2.shutdown()


def test_recheck_detects_corruption(tmp_path, swarm):
    tor, ses, files = swarm
    eng = TorrentEngine(LOCAL, tmp_path / "state")
    task = make_task(tmp_path, tor, eng)
    eng.add(task)
    drive(eng, task, lambda t, ev: t.status is DownloadStatus.COMPLETED, ses.listen_port())
    target = tmp_path / "dl" / "Pack" / "Game" / "data.bin"
    raw = bytearray(target.read_bytes())
    raw[1000:1010] = b"CORRUPTED!"
    target.write_bytes(bytes(raw))
    task.status = DownloadStatus.CHECKING
    eng.recheck(task)
    deadline = time.time() + 15
    seen_incomplete = False
    while time.time() < deadline:
        eng.poll()
        st = eng.handles[task.id].status()
        if st.has_metadata and not st.is_finished and st.state not in (
                lt.torrent_status.states.checking_files, lt.torrent_status.states.checking_resume_data):
            seen_incomplete = True
            break
        time.sleep(0.1)
    assert seen_incomplete, "libtorrent should notice the corrupted piece after a recheck"
    eng.shutdown()


def test_magnet_without_peers_waits_for_metadata(tmp_path):
    eng = TorrentEngine(LOCAL, tmp_path / "state")
    task = DownloadTask(id=7, kind=DownloadKind.TORRENT, title="m",
                        uri="magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a&dn=x",
                        dest_dir=str(tmp_path / "dl"))
    eng.add(task)
    time.sleep(0.5)
    eng.poll()
    assert task.status is DownloadStatus.METADATA
    assert "metadata" in (task.status_detail or "").lower()
    eng.remove(task)
    eng.shutdown()


def test_disk_error_is_reported(tmp_path, swarm):
    tor, ses, files = swarm
    eng = TorrentEngine(LOCAL, tmp_path / "state")
    task = make_task(tmp_path, tor, eng)
    blocker = tmp_path / "dl"
    blocker.mkdir()
    (blocker / "Pack").write_text("a file where the torrent wants a folder")
    eng.add(task)
    drive(eng, task, lambda t, ev: t.status is DownloadStatus.FAILED, ses.listen_port(), timeout=20)
    assert task.error and ("error" in task.error.lower())
    eng.shutdown()
