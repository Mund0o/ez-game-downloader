#!/usr/bin/env bash
# Build a self-contained Linux bundle in dist/EZGameDownloader/ and a tarball.
# Requires Python 3.12 (or 3.13). Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")/.."

# never rebuild under a running copy: it would read half-replaced files (crash EZGD-PY-7E6D32, 2026-10-11)
if pgrep -f "$PWD/dist/EZGameDownloader/ezgd" >/dev/null 2>&1; then
    echo "EZ Game Downloader is running from dist/ — close it before building (rebuilding replaces its files)." >&2
    exit 3
fi

PY="${PYTHON:-python3.12}"
if [ ! -x .venv/bin/python ]; then
    if command -v uv >/dev/null; then uv venv --python 3.12 .venv; else "$PY" -m venv .venv; fi
fi
if command -v uv >/dev/null; then
    uv pip install --python .venv/bin/python -r requirements-dev.txt
else
    .venv/bin/python -m pip install -r requirements-dev.txt
fi

.venv/bin/python packaging/make_icons.py
.venv/bin/python packaging/update_adblock.py          # bundle the newest ad blocklist (never fails the build)
.venv/bin/python packaging/fetch_7zip.py              # bundle 7-Zip (encrypted RAR support), SHA-256 pinned
.venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/ezgd.spec

install -Dm644 packaging/build-assets/ezgd.png dist/EZGameDownloader/ezgd.png
install -Dm644 packaging/ezgd.desktop dist/EZGameDownloader/ezgd.desktop
install -Dm755 packaging/install.sh dist/EZGameDownloader/install.sh
install -Dm755 packaging/uninstall.sh dist/EZGameDownloader/uninstall.sh
if [ -x packaging/build-assets/tools/linux/7zzs ]; then
    install -Dm755 packaging/build-assets/tools/linux/7zzs dist/EZGameDownloader/tools/7zzs
    install -Dm644 packaging/build-assets/tools/linux/7-Zip-License.txt dist/EZGameDownloader/tools/7-Zip-License.txt
fi
tar -C dist -czf dist/EZGameDownloader-linux-x86_64.tar.gz EZGameDownloader
echo "Built dist/EZGameDownloader/ezgd and dist/EZGameDownloader-linux-x86_64.tar.gz"
