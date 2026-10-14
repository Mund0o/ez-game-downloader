"""GE-Proton on demand: release parsing, SHA-512 verified download, safe unpacking into compatibilitytools.d."""

import hashlib
import io
import json
import tarfile

import pytest

from ezgd.platform import proton_install as pi

TAG = "GE-Proton11-7"
TOP = f"{TAG}-x86_64"            # the folder inside the real GE-Proton11-7-x86_64.tar.gz (checked 2026-10-11)


def tarball(top=TOP, evil=False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data=b"", mode=0o644):
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size, ti.mode = len(data), mode
            tf.addfile(ti, io.BytesIO(data))
        add("proton", b"#!/usr/bin/env python3\n", 0o755)
        add("compatibilitytool.vdf", b'"compatibilitytools" {}')
        add("files/lib/wine/x86_64-windows/kernel32.dll", b"MZ")
        link = tarfile.TarInfo(f"{top}/files/lib64")
        link.type, link.linkname = tarfile.SYMTYPE, "lib"
        tf.addfile(link)
        if evil:
            bad = tarfile.TarInfo(f"{top}/escape")
            bad.type, bad.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tf.addfile(bad)
    return buf.getvalue()


def release_json(size: int) -> str:
    base = f"https://github.com/GloriousEggroll/proton-ge-custom/releases/download/{TAG}"
    return json.dumps({"tag_name": TAG, "assets": [
        {"name": f"{TAG}-aarch64.tar.gz", "browser_download_url": f"{base}/{TAG}-aarch64.tar.gz", "size": 1},
        {"name": f"{TAG}-x86_64.tar.gz", "browser_download_url": f"{base}/{TAG}-x86_64.tar.gz", "size": size},
        {"name": f"{TAG}-x86_64.sha512sum", "browser_download_url": f"{base}/{TAG}-x86_64.sha512sum", "size": 160}]})


def test_parse_and_unpack(tmp_path):
    rel = pi.parse_release(release_json(123).encode())
    assert rel.tag == TAG and rel.tar_name == f"{TAG}-x86_64.tar.gz" and rel.size == 123
    assert pi.parse_sha512("a" * 128 + f"  {TAG}-x86_64.tar.gz\n", rel.tar_name) == "a" * 128
    t = tmp_path / "ge.tar.gz"
    t.write_bytes(tarball())
    tool = pi.extract(t, tmp_path / "Steam", TAG)
    assert tool.name == TOP and (tool / "proton").is_file() and (tool / "files" / "lib64").is_symlink()
    assert pi.installed(tmp_path / "Steam") == [TOP] and pi.ge_version(TOP) == (11, 7)
    assert pi.is_installed(tmp_path / "Steam", TAG) and not pi.is_installed(tmp_path / "Steam", "GE-Proton11-8")
    t.write_bytes(tarball(top=TAG))                                       # older releases: folder = tag
    assert pi.extract(t, tmp_path / "SteamOld", TAG).name == TAG
    t.write_bytes(tarball(top="GE-Proton11-70-x86_64"))                   # a different release is refused
    with pytest.raises(pi.ProtonInstallError, match="Unexpected archive layout"):
        pi.extract(t, tmp_path / "Steam4", TAG)
    t.write_bytes(tarball(evil=True))
    with pytest.raises(pi.ProtonInstallError):
        pi.extract(t, tmp_path / "Steam2", TAG)
    t.write_bytes(tarball(top="something-else"))
    with pytest.raises(pi.ProtonInstallError, match="Unexpected archive layout"):
        pi.extract(t, tmp_path / "Steam3", TAG)


async def test_core_install(app_paths, tmp_path, server, monkeypatch):
    """Real download path (HTTP engine + SHA-512 check) against the local test server."""
    from ezgd.core.app_core import AppCore
    data = tarball()
    good = hashlib.sha512(data).hexdigest()
    sha = server.add("/ge.sha512sum", data=f"{good}  {TAG}-x86_64.tar.gz\n".encode(), content_type="text/plain")
    server.add("/ge.tar.gz", data=data, content_type="application/gzip")
    rel = json.loads(release_json(len(data)))
    rel["assets"][1]["browser_download_url"] = server.url("/ge.tar.gz")
    rel["assets"][2]["browser_download_url"] = server.url("/ge.sha512sum")
    server.add("/releases/latest", data=json.dumps(rel).encode(), content_type="application/json")
    monkeypatch.setattr(pi, "RELEASES", server.url("/releases/latest"))
    steam = tmp_path / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    core = AppCore(app_paths, allow_private_network=True, enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        core.settings.set("steam_root", str(steam))
        st = await core.ge_proton_status()
        assert st["latest"] == TAG and st["newer_available"] and st["installed"] == []
        sha.data = ("b" * 128 + f"  {TAG}-x86_64.tar.gz\n").encode()          # tampered: refused
        with pytest.raises(Exception, match="(?i)checksum"):
            await core.install_ge_proton()
        assert not (steam / "compatibilitytools.d" / TOP).exists()
        sha.data = f"{good}  {TAG}-x86_64.tar.gz\n".encode()
        assert await core.install_ge_proton() == TAG
        assert (steam / "compatibilitytools.d" / TOP / "proton").is_file()
        assert any(p.name == TOP for p in core.library.protons())
        assert await core.install_ge_proton() == TAG                      # already there: not downloaded again
        assert (await core.ge_proton_status())["newer_available"] is False
    finally:
        await core.shutdown()
