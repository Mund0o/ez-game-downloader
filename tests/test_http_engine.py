import asyncio
import hashlib
import time

import pytest

from conftest import random_bytes
from ezgd.downloads.http_engine import DownloadError, HttpEngine, filename_from_headers, parse_content_range
from ezgd.downloads.models import DownloadKind, DownloadTask, Verification
from ezgd.net.ratelimit import TokenBucket


async def no_sleep(_):
    await asyncio.sleep(0)


def task_for(url, dest, **kw):
    return DownloadTask(kind=DownloadKind.HTTP, title="test", uri=url, dest_dir=str(dest), **kw)


def engine(client, **kw):
    kw.setdefault("sleep", no_sleep)
    return HttpEngine(client, TokenBucket(0), **kw)


async def run(eng, task):
    progress, checkpoints = [], []
    path = await eng.run(task, lambda t: progress.append(t.downloaded_bytes),
                         lambda t: checkpoints.append(t.downloaded_bytes))
    return path, progress, checkpoints


async def test_full_download_with_progress(server, local_client, tmp_path):
    data = random_bytes(3 * 1024 * 1024)
    server.add("/game.zip", data=data, chunk_delay=0.002,
               headers={"Content-Disposition": 'attachment; filename="My Game.zip"'})
    task = task_for(server.url("/game.zip"), tmp_path)
    path, progress, checkpoints = await run(engine(local_client), task)
    assert path.name == "My Game.zip" and path.read_bytes() == data
    assert not list(tmp_path.glob("*.part"))
    assert task.total_bytes == len(data) and task.downloaded_bytes == len(data)
    assert progress and checkpoints
    assert task.verification is Verification.NONE        # no trusted checksum → not "verified"


async def test_resume_after_connection_drop(server, local_client, tmp_path):
    data = random_bytes(2 * 1024 * 1024, seed=2)
    res = server.add("/f.bin", data=data, etag='"abc"', drop_after=700_000, drop_times=2)
    task = task_for(server.url("/f.bin"), tmp_path)
    path, *_ = await run(engine(local_client), task)
    assert path.read_bytes() == data
    assert res.hits == 3
    assert res.range_requests and res.range_requests[0].startswith("bytes=")
    assert task.attempts == 2


async def test_server_without_range_support_restarts_cleanly(server, local_client, tmp_path):
    data = random_bytes(1_000_000, seed=3)
    server.add("/norange.bin", data=data, support_range=False, drop_after=300_000, drop_times=1)
    task = task_for(server.url("/norange.bin"), tmp_path)
    path, *_ = await run(engine(local_client), task)
    assert path.read_bytes() == data


async def test_changed_file_restarts_from_zero(server, local_client, tmp_path):
    data = random_bytes(500_000, seed=4)
    server.add("/c.bin", data=data, etag='"new"')
    task = task_for(server.url("/c.bin"), tmp_path, filename="c.bin")
    task.etag = '"old"'
    (tmp_path / "c.bin.part").write_bytes(b"stale-bytes" * 1000)
    path, *_ = await run(engine(local_client), task)
    assert path.read_bytes() == data


async def test_transient_errors_are_retried(server, local_client, tmp_path):
    data = b"hello" * 1000
    res = server.add("/flaky", data=data, status_sequence=[503, 500])
    task = task_for(server.url("/flaky"), tmp_path, filename="flaky.bin")
    path, *_ = await run(engine(local_client), task)
    assert path.read_bytes() == data and res.hits == 3


async def test_retry_budget_is_bounded(server, local_client, tmp_path):
    server.add("/down", status_sequence=[503] * 10)
    task = task_for(server.url("/down"), tmp_path, filename="x")
    with pytest.raises(DownloadError, match="gave up after 2 retries"):
        await run(engine(local_client, max_retries=2), task)


@pytest.mark.parametrize("code,msg", [(404, "no longer available"), (410, "expired"), (403, "Access denied")])
async def test_permanent_http_errors(server, local_client, tmp_path, code, msg):
    server.add("/gone", status_sequence=[code])
    task = task_for(server.url("/gone"), tmp_path, filename="x")
    with pytest.raises(DownloadError, match=msg):
        await run(engine(local_client), task)


async def test_html_interstitial_is_rejected(server, local_client, tmp_path):
    server.add("/captcha", data=b"<html>solve captcha</html>", content_type="text/html; charset=utf-8")
    task = task_for(server.url("/captcha"), tmp_path)
    with pytest.raises(DownloadError, match="web page instead of a file"):
        await run(engine(local_client), task)


async def test_checksum_verified_and_mismatch(server, local_client, tmp_path):
    data = random_bytes(200_000, seed=5)
    server.add("/sum.bin", data=data)
    good = task_for(server.url("/sum.bin"), tmp_path, filename="sum.bin",
                    expected_checksum="sha256:" + hashlib.sha256(data).hexdigest())
    await run(engine(local_client), good)
    assert good.verification is Verification.VERIFIED
    bad = task_for(server.url("/sum.bin"), tmp_path / "b", filename="sum.bin",
                   expected_checksum="sha256:" + "0" * 64)
    with pytest.raises(DownloadError, match="checksum mismatch"):
        await run(engine(local_client), bad)
    assert bad.verification is Verification.MISMATCH
    assert not list((tmp_path / "b").glob("*"))           # corrupted file removed


async def test_insufficient_disk_space(server, local_client, tmp_path, monkeypatch):
    server.add("/big.bin", data=b"x" * 10_000)
    import ezgd.core.fsutil as fsutil
    monkeypatch.setattr(fsutil, "free_space", lambda p: 1000)
    task = task_for(server.url("/big.bin"), tmp_path, filename="big.bin")
    with pytest.raises(DownloadError, match="Not enough free space"):
        await run(engine(local_client), task)


async def test_existing_file_is_not_overwritten(server, local_client, tmp_path):
    server.add("/same.bin", data=b"new")
    (tmp_path / "same.bin").write_bytes(b"old user file")
    task = task_for(server.url("/same.bin"), tmp_path, filename="same.bin")
    path, *_ = await run(engine(local_client), task)
    assert path.name == "same (1).bin" and path.read_bytes() == b"new"
    assert (tmp_path / "same.bin").read_bytes() == b"old user file"


async def test_redirect_to_file_followed(server, local_client, tmp_path):
    server.add("/real.bin", data=b"payload")
    server.add("/link", redirect_to=server.url("/real.bin"))
    task = task_for(server.url("/link"), tmp_path)
    path, *_ = await run(engine(local_client), task)
    assert path.read_bytes() == b"payload" and path.name == "real.bin"


async def test_bandwidth_limit(server, local_client, tmp_path):
    data = random_bytes(600_000, seed=6)
    server.add("/slow.bin", data=data)
    eng = HttpEngine(local_client, TokenBucket(400_000), sleep=no_sleep)
    task = task_for(server.url("/slow.bin"), tmp_path, filename="slow.bin")
    t0 = time.monotonic()
    await run(eng, task)
    assert time.monotonic() - t0 >= 1.0      # 600 KB at 400 KB/s with 200 KB burst


async def test_cancellation_keeps_partial_file(server, local_client, tmp_path):
    data = random_bytes(4_000_000, seed=7)
    server.add("/long.bin", data=data, chunk_delay=0.01)
    task = task_for(server.url("/long.bin"), tmp_path, filename="long.bin")
    job = asyncio.create_task(run(engine(local_client), task))
    while task.downloaded_bytes < 100_000:
        await asyncio.sleep(0.02)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    part = tmp_path / "long.bin.part"
    assert part.exists() and 0 < part.stat().st_size < len(data)


def test_header_helpers():
    import httpx
    h = httpx.Headers({"content-disposition": "attachment; filename*=UTF-8''Gr%C3%BC%C3%9Fe.zip"})
    assert filename_from_headers(h, "https://x/y") == "Grüße.zip"
    assert filename_from_headers(httpx.Headers(), "https://x/files/My%20Game.rar") == "My Game.rar"
    assert parse_content_range("bytes 100-199/1000") == (100, 1000)
    assert parse_content_range("bytes 0-1/*") == (0, None)
    assert parse_content_range("garbage") is None


def test_safe_filename():
    from ezgd.core.fsutil import safe_filename
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\win.ini") == "win.ini"
    assert safe_filename("CON.txt") == "_CON.txt"
    assert safe_filename('a<b>:c"|?*.zip') == "a_b__c____.zip"
    assert safe_filename("") == "download"
    assert len(safe_filename("x" * 500 + ".rar").encode()) <= 200
