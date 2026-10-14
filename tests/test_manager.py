"""Download queue: concurrency, commands, persistence and restart recovery."""

import asyncio

import pytest

from conftest import random_bytes
from ezgd.database.repositories import DownloadRepository
from ezgd.downloads.http_engine import HttpEngine
from ezgd.downloads.manager import DownloadCommandError, DownloadManager, DuplicateDownloadError
from ezgd.downloads.models import DownloadStatus
from ezgd.net.ratelimit import TokenBucket


async def no_sleep(_):
    await asyncio.sleep(0)


class Events:
    def __init__(self):
        self.items = []

    def __call__(self, name, payload):
        self.items.append((name, payload))

    def names(self):
        return [n for n, _ in self.items]


def build(db, settings, client, completed=None):
    events = Events()
    eng = HttpEngine(client, TokenBucket(0), sleep=no_sleep)
    m = DownloadManager(DownloadRepository(db), settings, eng, None, events, client=client,
                        on_completed=completed)
    return m, events


async def wait_for(pred, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


async def test_concurrency_limit_and_completion(db, settings, server, local_client, tmp_path):
    settings.set("max_concurrent_downloads", 1)
    for i in range(3):
        server.add(f"/f{i}", data=random_bytes(400_000, seed=i), chunk_delay=0.003)
    done = []

    async def completed(task, path):
        done.append((task.id, path))
    m, events = build(db, settings, local_client, completed)
    await m.start()
    try:
        ids = [(await m.add_http(server.url(f"/f{i}"), title=f"g{i}", dest_dir=str(tmp_path / "dl"),
                                 filename=f"f{i}.bin"))["id"] for i in range(3)]
        max_active = 0
        while len(done) < 3:
            max_active = max(max_active, m._active_count())
            await asyncio.sleep(0.02)
        assert max_active == 1
        assert [d[0] for d in done] == ids                 # FIFO at equal priority
        for i in range(3):
            assert (tmp_path / "dl" / f"f{i}.bin").read_bytes() == random_bytes(400_000, seed=i)
        assert "download_completed" in events.names()
    finally:
        await m.shutdown()


async def test_pause_resume_cancel_retry_remove(db, settings, server, local_client, tmp_path):
    data = random_bytes(3_000_000, seed=9)
    server.add("/big", data=data, chunk_delay=0.01)
    m, events = build(db, settings, local_client)
    await m.start()
    try:
        snap = await m.add_http(server.url("/big"), title="Big", dest_dir=str(tmp_path), filename="big.bin")
        tid = snap["id"]
        await wait_for(lambda: m.tasks[tid].downloaded_bytes > 200_000)
        await m.pause(tid)
        assert m.tasks[tid].status is DownloadStatus.PAUSED
        paused_at = (tmp_path / "big.bin.part").stat().st_size
        await asyncio.sleep(0.3)
        assert (tmp_path / "big.bin.part").stat().st_size == paused_at
        await m.resume(tid)
        await wait_for(lambda: m.tasks[tid].status is DownloadStatus.COMPLETED)
        assert (tmp_path / "big.bin").read_bytes() == data

        server.add("/big2", data=data, chunk_delay=0.01)
        t2 = (await m.add_http(server.url("/big2"), title="Big2", dest_dir=str(tmp_path), filename="b2.bin"))["id"]
        await wait_for(lambda: m.tasks[t2].downloaded_bytes > 100_000)
        await m.cancel(t2)
        assert m.tasks[t2].status is DownloadStatus.CANCELLED and not (tmp_path / "b2.bin.part").exists()
        await m.retry(t2)
        await wait_for(lambda: m.tasks[t2].status is DownloadStatus.COMPLETED)

        deleted = await m.remove(t2, delete_files=True)
        assert deleted and not (tmp_path / "b2.bin").exists()
        assert t2 not in m.tasks and DownloadRepository(db).get(t2) is None
        with pytest.raises(DownloadCommandError):
            await m.pause(tid)                              # already finished
    finally:
        await m.shutdown()


async def test_failures_are_recorded_with_message(db, settings, server, local_client, tmp_path):
    server.add("/missing", status_sequence=[404])
    m, events = build(db, settings, local_client)
    await m.start()
    try:
        tid = (await m.add_http(server.url("/missing"), title="Gone", dest_dir=str(tmp_path), filename="x"))["id"]
        await wait_for(lambda: m.tasks[tid].status is DownloadStatus.FAILED)
        assert "no longer available" in m.tasks[tid].error
        assert DownloadRepository(db).get(tid).error == m.tasks[tid].error
        assert ("notify" in events.names())
    finally:
        await m.shutdown()


async def test_duplicates_and_invalid_input_rejected(db, settings, server, local_client, tmp_path):
    server.add("/slow", data=b"x" * 2_000_000, chunk_delay=0.05)
    m, _ = build(db, settings, local_client)
    await m.start()
    try:
        await m.add_http(server.url("/slow"), title="a", dest_dir=str(tmp_path))
        with pytest.raises(DuplicateDownloadError):
            await m.add_http(server.url("/slow"), title="a", dest_dir=str(tmp_path))
        with pytest.raises(DownloadCommandError):
            await m.add_http("file:///etc/passwd", title="x", dest_dir=str(tmp_path))
        with pytest.raises(DownloadCommandError):
            await m.add_http(server.url("/slow?2"), title="x", dest_dir="relative/path")
        with pytest.raises(DownloadCommandError):
            await m.add_from_text("not a link at all", dest_dir=str(tmp_path))
    finally:
        await m.shutdown()


async def test_restart_recovery_resumes_partial_download(db, settings, server, local_client, tmp_path):
    data = random_bytes(4_000_000, seed=21)
    res = server.add("/resume", data=data, etag='"e1"', chunk_delay=0.01)
    m, _ = build(db, settings, local_client)
    await m.start()
    tid = (await m.add_http(server.url("/resume"), title="R", dest_dir=str(tmp_path), filename="r.bin"))["id"]
    await wait_for(lambda: m.tasks[tid].downloaded_bytes > 500_000)
    await m.shutdown()                       # simulates closing the app mid-download
    stored = DownloadRepository(db).get(tid)
    assert stored.status is DownloadStatus.DOWNLOADING
    part_size = (tmp_path / "r.bin.part").stat().st_size
    assert part_size > 0

    res.chunk_delay = 0.0
    m2, _ = build(db, settings, local_client)
    await m2.start()
    try:
        assert m2.tasks[tid].downloaded_bytes == part_size
        await wait_for(lambda: m2.tasks[tid].status is DownloadStatus.COMPLETED)
        assert (tmp_path / "r.bin").read_bytes() == data
        assert any(r.startswith(f"bytes={part_size}-") for r in res.range_requests)
    finally:
        await m2.shutdown()


async def test_paused_state_survives_restart(db, settings, server, local_client, tmp_path):
    server.add("/p", data=b"x" * 3_000_000, chunk_delay=0.02)
    m, _ = build(db, settings, local_client)
    await m.start()
    tid = (await m.add_http(server.url("/p"), title="P", dest_dir=str(tmp_path), filename="p.bin"))["id"]
    await wait_for(lambda: m.tasks[tid].downloaded_bytes > 0)
    await m.pause(tid)
    await m.shutdown()
    m2, _ = build(db, settings, local_client)
    await m2.start()
    try:
        await asyncio.sleep(0.3)
        assert m2.tasks[tid].status is DownloadStatus.PAUSED
    finally:
        await m2.shutdown()


async def test_priority_moves_task_ahead(db, settings, server, local_client, tmp_path):
    settings.set("max_concurrent_downloads", 1)
    server.add("/blocker", data=b"x" * 1_000_000, chunk_delay=0.01)
    server.add("/low", data=b"l" * 1000)
    server.add("/high", data=b"h" * 1000)
    order = []

    async def completed(task, path):
        order.append(task.title)
    m, _ = build(db, settings, local_client, completed)
    await m.start()
    try:
        b = await m.add_http(server.url("/blocker"), title="blocker", dest_dir=str(tmp_path))
        await wait_for(lambda: m.tasks[b["id"]].status is DownloadStatus.DOWNLOADING)
        await m.add_http(server.url("/low"), title="low", dest_dir=str(tmp_path))
        high = await m.add_http(server.url("/high"), title="high", dest_dir=str(tmp_path))
        await m.set_priority(high["id"], 5)
        await wait_for(lambda: len(order) == 3)
        assert order == ["blocker", "high", "low"]
    finally:
        await m.shutdown()
