"""App updates from a GitHub releases page.

* ``check``: asks GitHub's public API for the latest release of the configured repository (``owner/name``) and
  compares its tag (``v0.2.0``) with this version.
* ``download``: fetches the release asset for this platform and verifies it against the SHA-256 GitHub publishes
  for every uploaded asset (or a ``SHA256SUMS`` asset). Without a checksum nothing is installed.
* ``stage``: unpacks it next to the installed app and runs the new build's ``--version`` to prove it starts.
* ``schedule_swap``: after the app has quit, a small script swaps the folders (the old one is kept until the new
  version has started), then starts the new version. If the swap fails, the old version is put back.

Only packaged builds update themselves; a copy run from source says so instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

from ezgd import __version__
from ezgd.library.updates import compare

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/releases/latest"
REPO_API = "https://api.github.com/repos/{repo}"
DEFAULT_REPO = "Mund0o/ez-game-downloader"       # the official releases; Settings can point elsewhere
ASSETS = {"linux": "EZGameDownloader-linux-x86_64.tar.gz", "windows": "EZGameDownloader-windows-x64.zip"}
NEW_SUFFIX, OLD_SUFFIX = ".update-new", ".update-old"
MAX_ASSET = 1024 * 1024 * 1024


class UpdateError(Exception):
    pass


def platform_key() -> str:
    return "windows" if os.name == "nt" else "linux"


def parse_repo(text: str) -> str:
    """``owner/name`` from "owner/name", "github.com/owner/name" or a releases URL."""
    t = (text or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$", t) or \
        re.match(r"^([\w.-]+)/([\w.-]+?)(?:\.git)?$", t)
    if not m or m.group(1) in (".", "..") or m.group(2) in (".", ".."):
        raise UpdateError(f"Enter the GitHub repository as owner/name (for example {DEFAULT_REPO}) or its "
                          "github.com link.")
    return f"{m.group(1)}/{m.group(2)}"


def install_dir() -> Path | None:
    """The packaged app's folder (the one with the ``ezgd`` executable), or None when run from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def version_of_tag(tag: str) -> str:
    return re.sub(r"^[vV]", "", (tag or "").strip())


async def check(client, repo: str) -> dict:
    repo = parse_repo(repo)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    res = await client.fetch(API.format(repo=repo), max_bytes=2 * 1024 * 1024, headers=headers)
    if res.status == 404 and (await client.fetch(REPO_API.format(repo=repo), max_bytes=2 * 1024 * 1024,
                                                 headers=headers)).status == 200:
        # the repository exists but has not published a release yet: nothing newer than this version
        return {"repo": repo, "tag": "", "version": "", "current": __version__, "newer": False, "no_release": True,
                "name": "", "notes": "", "published": None, "page": f"https://github.com/{repo}/releases",
                "asset": None, "asset_url": None, "size": None, "sha256": None, "sums_url": None,
                "checked_at": time.time()}
    if res.status == 404:
        raise UpdateError(f"GitHub has no published release for {repo} (or the repository is private or does not "
                          "exist).")
    if res.status == 403:
        raise UpdateError("GitHub refused the request (rate limit) — try again in an hour.")
    if res.status != 200:
        raise UpdateError(f"GitHub answered HTTP {res.status}.")
    try:
        rel = json.loads(res.content)
    except ValueError as exc:
        raise UpdateError("GitHub's answer could not be read.") from exc
    tag = str(rel.get("tag_name") or "")
    version = version_of_tag(tag)
    want = ASSETS[platform_key()]
    assets = rel.get("assets") or []
    asset = next((a for a in assets if a.get("name") == want), None)
    sums = next((a for a in assets if str(a.get("name", "")).upper() in ("SHA256SUMS", "SHA256SUMS.TXT")), None)
    sig = next((a for a in assets if a.get("name") == want + ".sig"), None)
    digest = str((asset or {}).get("digest") or "")
    c = compare(version, __version__)
    return {"repo": repo, "tag": tag, "version": version, "current": __version__,
            "newer": bool(c is not None and c > 0 and not rel.get("draft") and not rel.get("prerelease")),
            "name": rel.get("name") or tag, "notes": (rel.get("body") or "")[:4000],
            "published": rel.get("published_at"), "page": rel.get("html_url"),
            "asset": want if asset else None, "asset_url": (asset or {}).get("browser_download_url"),
            "size": (asset or {}).get("size"),
            "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
            "sums_url": (sums or {}).get("browser_download_url"), "sig_url": (sig or {}).get("browser_download_url"),
            "checked_at": time.time()}


async def _expected_sha(client, info: dict) -> str:
    if info.get("sha256"):
        return info["sha256"]
    if info.get("sums_url"):
        res = await client.fetch(info["sums_url"], max_bytes=256 * 1024)
        if res.status == 200:
            for line in res.content.decode("utf-8", "replace").splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1].lstrip("*") == info["asset"] and re.fullmatch(r"[0-9a-fA-F]{64}",
                                                                                                parts[0]):
                    return parts[0].lower()
    raise UpdateError("This release publishes no SHA-256 checksum for its download, so the app does not install "
                      "it by itself. Download it from the release page instead.")


async def download(client, info: dict, folder: Path, progress=None) -> Path:
    if not info.get("asset_url"):
        raise UpdateError(f"The release has no {ASSETS[platform_key()]} file for this system.")
    expected = await _expected_sha(client, info)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / info["asset"]
    part = target.with_suffix(target.suffix + ".part")
    h = hashlib.sha256()
    got = 0
    async with client.stream(info["asset_url"], headers={"Accept": "application/octet-stream"}) as resp:
        if resp.status_code != 200:
            raise UpdateError(f"The download answered HTTP {resp.status_code}.")
        total = int(resp.headers.get("content-length") or info.get("size") or 0)
        if total > MAX_ASSET:
            raise UpdateError("The update file is unexpectedly large; not downloaded.")
        with open(part, "wb") as fh:
            async for chunk in resp.aiter_bytes(256 * 1024):
                got += len(chunk)
                if got > MAX_ASSET:
                    raise UpdateError("The update file is unexpectedly large; not downloaded.")
                h.update(chunk)
                fh.write(chunk)
                if progress:
                    progress(got, total)
    if h.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded update does not match its published SHA-256 — it was deleted, nothing "
                          "was changed.")
    try:
        await _check_signature(client, info, h.hexdigest())
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, target)
    return target


async def _check_signature(client, info: dict, sha256: str) -> None:
    """The release must be signed with the developer's release key (release_keys): the SHA-256 only proves the file is
    the one on the release page — a stolen GitHub account could put any file there."""
    from ezgd.core import release_keys
    sig = ""
    if info.get("sig_url"):
        res = await client.fetch(info["sig_url"], max_bytes=4096)
        if res.status == 200:
            sig = res.content.decode("ascii", "replace")
    if not sig:
        raise UpdateError("This release is not signed, so the app does not install it by itself (a signature proves "
                          "the developer made it). Download it from the release page only if you trust it.")
    if not release_keys.verify(sig, info["asset"], info.get("version") or "", sha256):
        raise UpdateError("This release's signature is not valid — it was not made with the developer's release key. "
                          "Nothing was installed; the download was deleted.")


def _staging_paths(app: Path) -> tuple[Path, Path]:
    return app.with_name(f".{app.name}{NEW_SUFFIX}"), app.with_name(f".{app.name}{OLD_SUFFIX}")


def stage(archive: Path, app: Path, expected_version: str) -> Path:
    """Unpack the update next to ``app`` and make sure it starts and reports ``expected_version``."""
    new, _old = _staging_paths(app)
    if new.exists():
        shutil.rmtree(new, ignore_errors=True)
    unpack = new.with_name(new.name + ".tmp")
    shutil.rmtree(unpack, ignore_errors=True)
    unpack.mkdir(parents=True)
    try:
        if archive.name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(unpack, filter="data")
        else:
            with zipfile.ZipFile(archive) as zf:
                for n in zf.namelist():
                    p = Path(n)
                    if p.is_absolute() or ".." in p.parts:
                        raise UpdateError(f"Unsafe file name in the update: {n}")
                zf.extractall(unpack)
        inner = unpack / "EZGameDownloader"
        root = inner if inner.is_dir() else unpack
        exe = root / ("ezgd.exe" if os.name == "nt" else "ezgd")
        if not exe.is_file():
            raise UpdateError("The update does not contain the app.")
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        try:
            out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60, env=env,
                                 stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"The new version does not start: {exc}") from exc
        reported = (out.stdout + out.stderr).strip().split()[-1:] or [""]
        if version_of_tag(reported[0]) != version_of_tag(expected_version):
            raise UpdateError(f"The new version reports “{(out.stdout + out.stderr).strip()[:80]}”, expected "
                              f"{expected_version} — not installed.")
        os.replace(root, new)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"Could not unpack the update: {exc}") from exc
    finally:
        shutil.rmtree(unpack, ignore_errors=True)
    return new


SWAP_SCRIPT = r"""#!/bin/sh
# EZ Game Downloader update: swap the app folder once the running app has quit.
pid="$1"; app="$2"; new="$3"; old="$4"; log="$5"; shift 5
exec >>"$log" 2>&1
echo "$(date) update: waiting for $pid to exit"
i=0
while kill -0 "$pid" 2>/dev/null; do
    i=$((i+1))
    if [ "$i" -gt 900 ]; then echo "app did not quit; update not applied"; exit 1; fi
    sleep 0.2
done
[ -d "$new" ] || { echo "staged update missing"; exit 1; }
rm -rf "$old"
if ! mv "$app" "$old"; then echo "could not move the old version aside"; exit 1; fi
if ! mv "$new" "$app"; then
    echo "could not move the new version in; restoring the old one"
    mv "$old" "$app"
    cd "$app" || cd /
    setsid "$app/ezgd" "$@" </dev/null >/dev/null 2>&1 &
    exit 1
fi
echo "$(date) update: swapped; starting the new version"
# (not from the folder the old app was started in: that folder is now the old version, deleted a minute later, and
# the new app's helper processes could not start from a deleted folder — "[Errno 2] No such file or directory")
cd "$app" || cd /
setsid "$app/ezgd" "$@" </dev/null >/dev/null 2>&1 &
exit 0
"""


def schedule_swap(app: Path, data_dir: Path, log_dir: Path, args: list[str], pid: int | None = None) -> None:
    """Start the swap script (it waits for this process — or ``pid`` — to exit). The caller then quits the app."""
    if os.name == "nt":
        raise UpdateError("Automatic installation is only available on Linux for now — download the update from "
                          "the release page.")
    new, old = _staging_paths(app)
    if not new.is_dir():
        raise UpdateError("The update is not ready; download it again.")
    script = Path(data_dir) / "apply-update.sh"
    script.write_text(SWAP_SCRIPT, "utf-8")
    script.chmod(0o700)
    subprocess.Popen(["/bin/sh", str(script), str(pid or os.getpid()), str(app), str(new), str(old),
                      str(Path(log_dir) / "update.log"), *args],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    log.info("update to %s scheduled: the app folder is swapped after quitting", new)


def staged_version(app: Path) -> str | None:
    new, _ = _staging_paths(app)
    exe = new / ("ezgd.exe" if os.name == "nt" else "ezgd")
    return "ready" if exe.is_file() else None


def cleanup_previous(app: Path | None) -> bool:
    """After a successful update: remove the previous version kept next to the app."""
    if app is None:
        return False
    _new, old = _staging_paths(app)
    if old.is_dir():
        shutil.rmtree(old, ignore_errors=True)
        log.info("removed the previous version %s", old)
        return True
    return False


def relaunch_args(argv: list[str]) -> list[str]:
    """The arguments to start the new version with (the user's --home, never one-shot/testing flags)."""
    out, it = [], iter(argv)
    for a in it:
        if a == "--home":
            nxt = next(it, None)
            if nxt:
                out += ["--home", os.path.abspath(nxt)]       # (the new version starts in another folder)
        elif a.startswith("--home="):
            out.append("--home=" + os.path.abspath(a.split("=", 1)[1]))
    return out
