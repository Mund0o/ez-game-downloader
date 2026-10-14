#!/usr/bin/env bash
# Publish the Linux build as a GitHub release, so installed copies can update themselves
# (Settings → App updates). Needs the GitHub CLI (`gh auth login` once).
#   packaging/build_linux.sh && packaging/publish_github.sh owner/repo
# The release is tagged v<version> from ezgd/__init__.py; bump the version before every release.
set -euo pipefail
cd "$(dirname "$0")/.."
repo="${1:?usage: packaging/publish_github.sh owner/repo}"
version=$(.venv/bin/python -c 'import ezgd; print(ezgd.__version__)')
tarball=dist/EZGameDownloader-linux-x86_64.tar.gz
[ -f "$tarball" ] || { echo "Build first: packaging/build_linux.sh" >&2; exit 1; }
built=$(tar -xzOf "$tarball" EZGameDownloader/ezgd >/dev/null 2>&1 && echo ok || true)
[ -n "$built" ] || { echo "$tarball does not contain EZGameDownloader/ezgd" >&2; exit 1; }
(cd dist && sha256sum EZGameDownloader-linux-x86_64.tar.gz > SHA256SUMS)
if gh release view "v$version" --repo "$repo" >/dev/null 2>&1; then
    echo "Release v$version already exists on $repo — bump __version__ in ezgd/__init__.py first." >&2
    exit 2
fi
gh release create "v$version" "$tarball" dist/SHA256SUMS --repo "$repo" \
    --title "EZ Game Downloader $version" --notes "${NOTES:-EZ Game Downloader $version}"
echo "Published v$version to https://github.com/$repo/releases"
