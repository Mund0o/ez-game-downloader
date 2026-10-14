"""Bundle 7-Zip's official Linux command-line build (``7zzs``) with the app (run by the build scripts).

Without UnRAR or 7-Zip installed, the app's only RAR fallback was bsdtar, which cannot open password-protected
RAR archives — every OnlineFix archive (2026-10-12: "bsdtar: Encryption is not supported"). 7-Zip reads RAR
(including RAR5, encrypted and multi-volume), 7z and zip. Its licence (GNU LGPL + unRAR restriction) allows
redistribution; License.txt is shipped next to it.

The download is pinned to one version and its SHA-256 (identical on www.7-zip.org and 7-Zip's official GitHub
releases, ip7z/7zip, checked 2026-10-12); anything else is refused. Already present → nothing is downloaded.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "26.04"
URL = "https://www.7-zip.org/a/7z2604-linux-x64.tar.xz"
MIRROR = "https://github.com/ip7z/7zip/releases/download/26.04/7z2604-linux-x64.tar.xz"
SHA256 = "fc0327ba27e89bd086cf426dff17d77de582953cdbbc10a6576540a06853ffcd"
OUT = Path(__file__).resolve().parent / "build-assets" / "tools" / "linux"


def main() -> int:
    exe = OUT / "7zzs"
    marker = OUT / "VERSION"
    if exe.is_file() and marker.is_file() and marker.read_text().strip() == VERSION:
        print(f"7-Zip {VERSION} already bundled")
        return 0
    data = None
    for url in (URL, MIRROR):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EZGameDownloader-build"})
            with urllib.request.urlopen(req, timeout=60) as r:
                blob = r.read(16 * 1024 * 1024)
        except Exception as exc:
            print(f"7-Zip download from {url} failed: {exc}")
            continue
        if hashlib.sha256(blob).hexdigest() != SHA256:
            print(f"7-Zip download from {url} does not match the pinned SHA-256 — refused")
            continue
        data = blob
        break
    if data is None:
        print("7-Zip was NOT bundled: RAR archives will need UnRAR or 7-Zip installed on the computer", file=sys.stderr)
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tf:
        for name, target in (("7zzs", "7zzs"), ("License.txt", "7-Zip-License.txt"), ("readme.txt", "7-Zip-readme.txt")):
            member = tf.getmember(name)
            (OUT / target).write_bytes(tf.extractfile(member).read())
    exe.chmod(0o755)
    (OUT / "VERSION").write_text(VERSION + "\n")
    print(f"7-Zip {VERSION} bundled ({exe})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
