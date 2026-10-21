"""Other DXVK / VKD3D-Proton versions through a Proton variant (links to the real Proton, one layer swapped)."""

import io
import json
import tarfile

from ezgd.library import dxlayers


def release(version, digest=True):
    a = {"name": f"dxvk-{version}.tar.gz", "browser_download_url": f"https://x/dxvk-{version}.tar.gz", "size": 1}
    if digest:
        a["digest"] = "sha256:" + "ab" * 32
    return {"tag_name": f"v{version}", "assets": [a]}


def test_releases_are_picked_around_the_bundled_version():
    body = json.dumps([release("3.1"), release("3.0.2"), release("3.0.1"), release("2.7.1"), release("2.7", False)])
    rels = dxlayers.parse_releases("dxvk", body)
    assert [r["version"] for r in rels] == ["3.1", "3.0.2", "3.0.1", "2.7.1"]        # unverifiable 2.7 left out
    assert [r["version"] for r in dxlayers.pick(rels, "3.0.2")] == ["3.1", "3.0.1"]
    assert [r["version"] for r in dxlayers.pick(rels, "3.1")] == ["3.0.2", "2.7.1"]
    assert dxlayers.kind_for_module("D3D12Core.dll") == "vkd3d" and dxlayers.kind_for_module("dxgi.dll") == "dxvk"
    assert dxlayers.kind_for_module("game.exe") is None


def test_a_variant_swaps_one_layer_and_links_the_rest(tmp_path):
    base = tmp_path / "Proton-GE Latest"
    for arch in ("x86_64-windows", "i386-windows"):
        d = base / "files" / "lib" / "wine" / "dxvk" / arch
        d.mkdir(parents=True)
        (d / "d3d11.dll").write_bytes(b"old v3.0.2")
        (d / "openvr_api_dxvk.dll").write_bytes(b"vr")
        (base / "files" / "lib" / "wine" / arch).mkdir()
        (base / "files" / "lib" / "wine" / arch / "kernel32.dll").write_bytes(b"k")
    (base / "proton").write_text("#!/usr/bin/env python3\nCURRENT_PREFIX_VERSION=\"GE-Proton11-5\"\n")
    (base / "version").write_text("1 GE-Proton11-5\n")
    (base / "toolmanifest.vdf").write_text('"manifest" { "require_tool_appid" "1628350" }')
    assert dxlayers.bundled_version(base, "dxvk") == "3.0.2"
    # a release archive: dxvk-3.1/x64, x32
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for arch in ("x64", "x32"):
            data = b"new 3.1 " + arch.encode()
            info = tarfile.TarInfo(f"dxvk-3.1/{arch}/d3d11.dll")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    archive = tmp_path / "dxvk-3.1.tar.gz"
    archive.write_bytes(buf.getvalue())
    layer = dxlayers.unpack(archive, "dxvk", "3.1", tmp_path / "layers")
    v = dxlayers.make_variant(base, "Proton-GE Latest", "dxvk", "3.1", layer, tmp_path / "variants")
    swapped = v / "files" / "lib" / "wine" / "dxvk" / "x86_64-windows"
    assert (swapped / "d3d11.dll").read_bytes() == b"new 3.1 x64" and not (swapped / "d3d11.dll").is_symlink()
    assert (swapped / "openvr_api_dxvk.dll").is_symlink()                            # what the release lacks
    assert (v / "files" / "lib" / "wine" / "x86_64-windows").is_symlink()            # everything else: links
    assert (v / "proton").is_file() and not (v / "proton").is_symlink()               # Proton finds itself here
    found = dxlayers.variants(tmp_path / "variants")
    assert [p.name for p in found] == ["Proton-GE Latest + DXVK 3.1"]
    assert found[0].require_tool_appid == 1628350                                     # the same runtime as its base
    assert dxlayers.base_of(v) == base.resolve()
    from ezgd.platform import prefixguard as pg
    assert pg.proton_prefix_version(v) == pg.proton_prefix_version(base)               # shares the game's prefix
