"""Third-party file hosts: classification and the few hosts that offer a public API.

Observed on 2026-10-09 (see README "Source integration notes"):

* Pixeldrain — documented public JSON API (``/api/file/<id>/info``) exposing
  size, SHA-256 and an ``availability`` flag that announces captcha
  requirements. Supported when ``availability`` is empty.
* Buzzheavier — Cloudflare browser check, and (verified 2026-10-10) it also bounces visits that do not come
  from the source's page to steamrip.com. Browser only, opened via the game page.
* FileDitch — redirects non-browser clients through a gate, and (verified 2026-10-10) bounces any visit
  that does not come from the source's page to steamrip.com. Browser only, opened via the game page.
* Gofile — downloads need an account token. Browser only.
* MegaDB / FileKeeper / VikingFile / others — waiting pages / captchas. Browser only.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from ezgd.catalog.models import DownloadMethod, MethodKind
from ezgd.sources.base import ManualActionRequired, ResolvedDownload, SourceError
from ezgd.sources.cache import CachedFetcher

_PIXELDRAIN_HOSTS = {"pixeldrain.com", "pixeldrain.net", "pixeldra.in"}
_PIXELDRAIN_FILE = re.compile(r"^/(?:u|api/file)/([A-Za-z0-9]{6,32})/?$")

HOST_NOTES = {
    "gofile.io": "Gofile only serves files to browsers with an account token.",
    "buzzheavier.com": "Buzzheavier checks for a real browser (Cloudflare) and only opens when you click it on the game's "
                       "page (opened directly it sends you to the SteamRIP home page), so the app opens that page — "
                       "click “Buzzheavier” there.",
    "bzzhr.to": "Buzzheavier checks for a real browser (Cloudflare) and only opens when you click it on the game's "
                       "page (opened directly it sends you to the SteamRIP home page), so the app opens that page — "
                       "click “Buzzheavier” there.",
    "bzzhr.co": "Buzzheavier checks for a real browser (Cloudflare) and only opens when you click it on the game's "
                       "page (opened directly it sends you to the SteamRIP home page), so the app opens that page — "
                       "click “Buzzheavier” there.",
    "fileditchfiles.st": "FileDitch only opens when you click it on the game's page (opened directly it sends "
                         "you to the SteamRIP home page), so the app opens that page — click “FileDitch” there.",
    "fileditch.com": "FileDitch only opens when you click it on the game's page (opened directly it sends "
                     "you to the SteamRIP home page), so the app opens that page — click “FileDitch” there.",
    "megadb.net": "MegaDB requires solving a captcha.",
    "filekeeper.net": "FileKeeper uses a waiting page and captcha.",
    "vikingfile.com": "VikingFile uses a waiting page and captcha.",
    "1fichier.com": "1fichier uses a waiting page and captcha.",
    "mega.nz": "MEGA downloads require its browser client.",
    "mediafire.com": "MediaFire download pages require a browser.",
}

HOST_LABELS = {
    "gofile.io": "Gofile", "buzzheavier.com": "Buzzheavier", "bzzhr.to": "Buzzheavier",
    "bzzhr.co": "Buzzheavier", "fileditchfiles.st": "FileDitch", "fileditch.com": "FileDitch",
    "megadb.net": "MegaDB", "filekeeper.net": "FileKeeper", "vikingfile.com": "VikingFile",
    "pixeldrain.com": "Pixeldrain", "pixeldrain.net": "Pixeldrain", "pixeldra.in": "Pixeldrain",
    "1fichier.com": "1fichier", "mega.nz": "MEGA", "mediafire.com": "MediaFire",
    "datanodes.to": "DataNodes", "fuckingfast.co": "FuckingFast",
}


# hosts that only serve a link when the visit comes from the page that lists it (Referer check); the app never
# fakes that, it opens the source's game page instead so the user's own click comes from there
_NEEDS_SOURCE_PAGE = {"fileditchfiles.st", "fileditch.com", "fileditch.ch", "fileditchfiles.me",
                      "buzzheavier.com", "bzzhr.to", "bzzhr.co"}


def opens_from_source_page(url: str) -> bool:
    return _host(url) in _NEEDS_SOURCE_PAGE


def browser_url(method: DownloadMethod, game_url: str | None) -> str:
    """What to open in the browser for a browser-only method."""
    return game_url if game_url and opens_from_source_page(method.url) else method.url


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def pixeldrain_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host not in _PIXELDRAIN_HOSTS:
        return None
    m = _PIXELDRAIN_FILE.match(parts.path)
    return m.group(1) if m else None


def classify_host_link(url: str, *, label: str | None = None, file_name: str | None = None,
                       dangerous: bool = False, archive_password: str | None = None) -> DownloadMethod:
    """Build a DownloadMethod for a file-host link, marking what the app can do itself."""
    if url.startswith("//"):
        url = "https:" + url
    host = _host(url)
    name = HOST_LABELS.get(host, host or "link")
    if pixeldrain_id(url):
        return DownloadMethod(MethodKind.DIRECT, label or name, url, host=host, supported=True,
                              resolver="pixeldrain", file_name=file_name, archive_password=archive_password,
                              flagged_dangerous=dangerous,
                              note="Downloaded through Pixeldrain's public API; SHA-256 verified when provided.")
    note = HOST_NOTES.get(host, "This host is not supported for in-app downloads; open it in your browser.")
    if dangerous:
        note = "The source warns that this mirror shows deceptive ads. " + note
    return DownloadMethod(MethodKind.EXTERNAL, label or name, url, host=host, supported=False, note=note,
                          file_name=file_name, flagged_dangerous=dangerous, archive_password=archive_password)


async def resolve_pixeldrain(fetcher: CachedFetcher, method: DownloadMethod) -> ResolvedDownload:
    file_id = pixeldrain_id(method.url)
    if not file_id:
        raise SourceError("not a Pixeldrain file link")
    info_url = f"https://pixeldrain.com/api/file/{file_id}/info"
    res = await fetcher.get(info_url, ttl=60, max_bytes=256 * 1024, force=True)
    if res.status == 404:
        raise SourceError("Pixeldrain reports that this file no longer exists (HTTP 404).")
    if res.status != 200:
        raise SourceError(f"Pixeldrain info request failed (HTTP {res.status}).")
    try:
        info = json.loads(res.body)
    except ValueError as exc:
        raise SourceError("Pixeldrain returned an unexpected response.") from exc
    if not info.get("success", True) or not info.get("can_download", True):
        raise ManualActionRequired("Pixeldrain does not allow this file to be downloaded right now.", method.url)
    if info.get("availability"):
        msg = info.get("availability_message") or info["availability"]
        raise ManualActionRequired(f"Pixeldrain requires browser interaction: {msg}", method.url)
    sha = info.get("hash_sha256")
    checksum = f"sha256:{sha.lower()}" if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha) else None
    size = info.get("size") if isinstance(info.get("size"), int) else None
    return ResolvedDownload("http", url=f"https://pixeldrain.com/api/file/{file_id}?download",
                            file_name=info.get("name") or method.file_name, size_bytes=size,
                            checksum=checksum, archive_password=method.archive_password)
