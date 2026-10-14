"""Domain blocklist for the in-app browser (ads, pop-unders, trackers, malware hosts).

The bundled list is StevenBlack/hosts (unified adware + malware, MIT licence) plus a few pop-under networks seen
on file-host pages that it does not list yet. Matching is by host name and every parent domain, so
``a.b.popads.net`` is blocked by ``popads.net``. Sites the user is actually using (the game page, its file host)
are never blocked, even if a list would.

The set is built once and only read afterwards, so it is safe to use from Qt WebEngine's IO thread.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
from importlib import resources
from pathlib import Path

_DOMAIN = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,62}\.)+[a-z0-9-]{1,63}")

log = logging.getLogger(__name__)

# pop-under / redirect networks seen on file-host pages, missing from the bundled list (2026-10-10)
EXTRA = {
    "monetag.com", "profitablecpmrate.com", "highperformanceformat.com", "effectivegatecpm.com",
    "profitableratecpm.com", "highcpmgate.com", "toprevenuegate.com", "revenuecpmgate.com",
    "realizationnewestfangs.com", "pl24999.net", "alwingulla.com", "inpagepush.com", "pushub.net",
    "onclckmn.com", "onclickalgo.com", "onclickperformance.com", "pemsrv.com", "tsyndicate.com",
    "dolohen.com", "acscdn.com", "adsco.re", "popmyads.com", "popunder.net", "clickaine.com",
}

# never blocked: the sources and file hosts the app works with
ALWAYS_ALLOWED = {
    "online-fix.me", "steamrip.com", "cracked-games.org", "fitgirl-repacks.site", "datanodes.to", "ankergames.net",
    "fuckingfast.co", "pixeldrain.com", "pixeldrain.net", "gofile.io",
    "buzzheavier.com", "bzzhr.to", "bzzhr.co", "fileditchfiles.st", "fileditchfiles.me", "fileditch.com",
    "filekeeper.net", "vikingfile.com", "vik1ngfile.site", "1fichier.com", "mega.nz", "mediafire.com",
    "rootz.so", "megadb.net", "filecrypt.cc", "cloudflare.com", "challenges.cloudflare.com",
    "hcaptcha.com", "recaptcha.net", "www.google.com", "www.gstatic.com",   # the checks the user completes
}

SOURCE_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
MIN_DOMAINS = 30_000           # a real list has ~70k; anything much smaller is truncated or not the list
MAX_AGE_DAYS = 7               # the list is updated about weekly
LIST_FILE = "stevenblack-hosts.txt.gz"

_LOCK = threading.Lock()
_BLOCKED: frozenset[str] | None = None
_DATA_DIR: Path | None = None   # where downloaded updates are kept (set by the app at start)
_INFO: dict = {}                # what is loaded: source, list date, count


def set_data_dir(path: Path | None) -> None:
    global _DATA_DIR
    _DATA_DIR = Path(path) if path is not None else None


def _updated_file() -> Path | None:
    return _DATA_DIR / LIST_FILE if _DATA_DIR else None


def parse_hosts(text: str) -> tuple[set[str], str | None]:
    """Domains from a hosts file (``0.0.0.0 domain``) or a plain domain list; plus its ``# Date:`` line."""
    domains: set[str] = set()
    date = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if date is None and line.lower().startswith("# date:"):
                date = line.split(":", 1)[1].strip()
            continue
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1"):
            host = parts[1]
        elif len(parts) == 1:
            host = parts[0]
        else:
            continue
        host = host.lower().rstrip(".")
        if "." in host and host not in ("0.0.0.0", "localhost", "localhost.localdomain", "local", "broadcasthost") \
                and _DOMAIN.fullmatch(host):
            domains.add(host)
    return domains, date


def _read(blob: bytes) -> tuple[set[str], str | None]:
    return parse_hosts(gzip.decompress(blob).decode("utf-8", "replace"))


def _load() -> frozenset[str]:
    global _INFO
    domains: set[str] = set()
    info: dict = {}
    upd = _updated_file()
    if upd is not None and upd.is_file():
        try:
            domains, date = _read(upd.read_bytes())
            if len(domains) >= MIN_DOMAINS:
                info = {"source": "downloaded", "date": date, "path": str(upd)}
            else:
                domains = set()
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            log.warning("downloaded ad blocklist unreadable, using the bundled one: %s", exc)
            domains = set()
    if not domains:
        try:
            domains, date = _read(resources.files("ezgd.resources").joinpath(f"adblock/{LIST_FILE}").read_bytes())
            info = {"source": "bundled", "date": date}
        except (OSError, FileNotFoundError, ModuleNotFoundError) as exc:   # pragma: no cover - broken install
            log.warning("ad blocklist not available: %s", exc)
    domains |= EXTRA
    info["count"] = len(domains)
    _INFO = info
    return frozenset(domains)


def blocked_domains() -> frozenset[str]:
    global _BLOCKED
    if _BLOCKED is None:
        with _LOCK:
            if _BLOCKED is None:
                _BLOCKED = _load()
                log.info("ad blocker: %d domains (%s list, %s)", len(_BLOCKED), _INFO.get("source"),
                         _INFO.get("date"))
    return _BLOCKED


def reload() -> None:
    """Swap in the newest list (readers keep using the old set until the reference changes)."""
    global _BLOCKED
    with _LOCK:
        _BLOCKED = _load()


def info() -> dict:
    blocked_domains()
    return dict(_INFO)


def install_update(text: str) -> dict:
    """Validate a freshly downloaded list and make it the active one; raises ValueError if it is not usable."""
    domains, date = parse_hosts(text)
    if len(domains) < MIN_DOMAINS:
        raise ValueError(f"the downloaded list has only {len(domains)} domains (expected tens of thousands) — "
                         "kept the current list")
    upd = _updated_file()
    if upd is None:
        raise ValueError("no data folder for the ad blocklist")
    upd.parent.mkdir(parents=True, exist_ok=True)
    tmp = upd.with_suffix(".tmp")
    header = f"# StevenBlack/hosts — {SOURCE_URL}\n# Date: {date or 'unknown'}\n"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        fh.write(header + "\n".join(sorted(domains)) + "\n")
    os.replace(tmp, upd)
    reload()
    return info()


def _parents(host: str):
    host = host.lower().rstrip(".")
    parts = host.split(".")
    for i in range(len(parts) - 1):
        yield ".".join(parts[i:])


def is_allowed(host: str, extra_allowed: frozenset[str] | set[str] = frozenset()) -> bool:
    return any(p in ALWAYS_ALLOWED or p in extra_allowed for p in _parents(host))


def is_blocked(host: str, extra_allowed: frozenset[str] | set[str] = frozenset()) -> bool:
    """True if ``host`` (or a parent domain) is on the blocklist and not a site the user is using."""
    if not host or is_allowed(host, extra_allowed):
        return False
    blocked = blocked_domains()
    return any(p in blocked for p in _parents(host))
