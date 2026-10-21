"""Other versions of the DirectX translation layers — DXVK (Direct3D 8–11) and VKD3D-Proton (Direct3D 12).

When a game crashes inside d3d11.dll / dxgi.dll / d3d12core.dll, the bug is often in that layer's version: an
older or newer DXVK starts the game where the bundled one does not. Proton copies its own DXVK and VKD3D into the
prefix on every start (proton: ``for f in dxvkfiles: try_copy(...)``, outside the "prefix changed" check), so
another version cannot simply be put into the prefix. Instead the app makes a **Proton variant**: a folder (in its
data folder) that mirrors the installed Proton through symbolic links — only ``files/lib/wine/dxvk`` (or
``vkd3d-proton``) is a real folder with the other version, and the ``proton`` script is a copy (Proton finds its
files from the path it was started by: ``Proton(os.path.dirname(sys.argv[0]))``). The variant behaves exactly
like the Proton it mirrors, with that one layer swapped, and nothing of the real Proton is touched.

Releases come from the layers' GitHub pages, verified against the SHA-256 digest GitHub publishes for each file.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

KINDS = {
    "dxvk": {"repo": "doitsujin/dxvk", "dir": "dxvk", "label": "DXVK",
             "asset": re.compile(r"^dxvk-(\d+\.\d+(?:\.\d+)?)\.tar\.gz$"),
             "modules": {"d3d11.dll", "dxgi.dll", "d3d9.dll", "d3d10core.dll", "d3d8.dll", "d3d10.dll",
                         "d3d10_1.dll"}},
    "vkd3d": {"repo": "HansKristian-Work/vkd3d-proton", "dir": "vkd3d-proton", "label": "VKD3D-Proton",
              "asset": re.compile(r"^vkd3d-proton-(\d+\.\d+(?:\.\d+)?)\.tar\.zst$"),
              "modules": {"d3d12.dll", "d3d12core.dll"}},
}
ARCH = {"x86_64-windows": ("x64",), "i386-windows": ("x32", "x86")}
MARK = "ezgd-variant.json"


def kind_for_module(name: str) -> str | None:
    n = (name or "").lower()
    return next((k for k, v in KINDS.items() if n in v["modules"]), None)


def releases_url(kind: str) -> str:
    return f"https://api.github.com/repos/{KINDS[kind]['repo']}/releases?per_page=30"


def parse_releases(kind: str, body: bytes | str) -> list[dict]:
    """[{version, name, url, size, sha256}] newest first; releases without a published digest are left out (they
    cannot be verified)."""
    out = []
    try:
        data = json.loads(body)
    except ValueError:
        return out
    for rel in data if isinstance(data, list) else []:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        for a in rel.get("assets") or []:
            m = KINDS[kind]["asset"].match(a.get("name") or "")
            digest = a.get("digest") or ""
            if m and digest.startswith("sha256:"):
                out.append({"version": m.group(1), "name": a["name"], "url": a.get("browser_download_url"),
                            "size": a.get("size") or 0, "sha256": digest.split(":", 1)[1]})
                break
    return sorted(out, key=lambda r: _ver(r["version"]), reverse=True)


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def bundled_version(proton_dir: Path, kind: str) -> str:
    """The version the Proton build brings (read from the DLL's own version text, e.g. "v3.0.2")."""
    d = Path(proton_dir) / "files" / "lib" / "wine" / KINDS[kind]["dir"] / "x86_64-windows"
    dll = d / ("d3d11.dll" if kind == "dxvk" else "d3d12core.dll")
    try:
        data = dll.read_bytes()
    except OSError:
        return ""
    for m in re.finditer(rb"v(\d+\.\d+(?:\.\d+)?)", data):
        if 1 <= int(m.group(1).split(b".")[0]) <= 9:
            return m.group(1).decode()
    return ""


def pick(releases: list[dict], bundled: str, n: int = 2) -> list[dict]:
    """The versions worth testing against the bundled one: the newest (when it is another), the last of the line
    before (a regression in the new line), and the one just before the bundled."""
    out: list[dict] = []
    b = _ver(bundled) if bundled else ()
    newest = next((r for r in releases if _ver(r["version"]) != b), None)
    if newest and (not b or _ver(newest["version"]) > b):
        out.append(newest)
    if b:
        older_line = next((r for r in releases if _ver(r["version"])[:1] < b[:1]), None)
        just_before = next((r for r in releases if _ver(r["version"]) < b), None)
        for r in (just_before, older_line):
            if r is not None and r not in out:
                out.append(r)
    return out[:n]


def unpack(archive: Path, kind: str, version: str, root: Path) -> Path:
    """The release's DLLs into ``root/<kind>-<version>/{x86_64-windows,i386-windows}`` (safely: data filter)."""
    dest = root / f"{kind}-{version}"
    if (dest / "x86_64-windows").is_dir():
        return dest
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ezgd-layer-", dir=root))
    try:
        tar_path = archive
        if archive.name.endswith(".zst"):
            tar_path = tmp / "layer.tar"
            with open(tar_path, "wb") as out:
                subprocess.run(["zstd", "-d", "-c", str(archive)], stdout=out, check=True, timeout=120)
        with tarfile.open(tar_path) as tf:
            tf.extractall(tmp / "x", filter="data")
        src = next((p for p in (tmp / "x").iterdir() if p.is_dir()), None)
        if src is None:
            raise ValueError("the archive has no folder")
        stage = tmp / "out"
        for arch, names in ARCH.items():
            got = next((src / n for n in names if (src / n).is_dir()), None)
            if got is None:
                continue
            (stage / arch).mkdir(parents=True)
            for dll in got.glob("*.dll"):
                shutil.copy2(dll, stage / arch / dll.name)
        if not (stage / "x86_64-windows").is_dir():
            raise ValueError("no 64-bit DLLs in the release")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(stage), str(dest))
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def variant_name(base_name: str, kind: str, version: str) -> str:
    return f"{base_name} + {KINDS[kind]['label']} {version}"


def make_variant(base_dir: Path, base_name: str, kind: str, version: str, layer: Path, root: Path) -> Path:
    """A Proton that is ``base_dir`` with one layer swapped (links everywhere else)."""
    name = variant_name(base_name, kind, version)
    dest = root / re.sub(r"[^\w.+-]+", "_", name)
    if (dest / MARK).is_file():
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    swap = ("files", "lib", "wine", KINDS[kind]["dir"])

    def mirror(src: Path, dst: Path, depth: int) -> None:
        for entry in src.iterdir():
            target = dst / entry.name
            if depth < len(swap) and entry.name == swap[depth] and entry.is_dir():
                if depth == len(swap) - 1:            # the layer itself: the other version
                    target.mkdir()
                    for arch in ("x86_64-windows", "i386-windows"):
                        (target / arch).mkdir()
                        own = layer / arch
                        if own.is_dir():
                            for dll in own.glob("*.dll"):
                                shutil.copy2(dll, target / arch / dll.name)
                        base_arch = entry / arch          # what the release lacks (openvr_api_dxvk.dll) stays
                        if base_arch.is_dir():
                            for f in base_arch.iterdir():
                                if not (target / arch / f.name).exists():
                                    (target / arch / f.name).symlink_to(f.resolve())
                else:
                    target.mkdir()
                    mirror(entry, target, depth + 1)
            elif depth == 0 and entry.name == "proton":
                shutil.copy2(entry, target)           # (a copy: Proton locates its files from its own path)
            else:
                target.symlink_to(entry.resolve())
    mirror(Path(base_dir), dest, 0)
    (dest / MARK).write_text(json.dumps({"base": str(Path(base_dir).resolve()), "base_name": base_name,
                                         "kind": kind, "version": version, "name": name}), "utf-8")
    return dest


def variants(root: Path) -> list:
    """The Proton variants made so far (as ProtonInstall, named "<base> + DXVK 2.7")."""
    from ezgd.platform.steam import ProtonInstall
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            try:
                meta = json.loads((d / MARK).read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if Path(meta.get("base", "")).is_dir() and (d / "proton").is_file():
                out.append(ProtonInstall(meta["name"], d))
    return out


def base_of(proton_dir: Path) -> Path | None:
    try:
        return Path(json.loads((Path(proton_dir) / MARK).read_text("utf-8"))["base"])
    except (OSError, ValueError, KeyError):
        return None
