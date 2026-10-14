"""Filter lists for the in-app browser: EasyList-style network rules, pop-up rules and element hiding.

The domain list in :mod:`ezgd.net.adblock` only knows host names. Ads on file-host pages also come from
first-party paths (``/js/pop.js``), rotating domains and in-page elements, which only full filter lists describe.
This module runs them with Brave's adblock-rust engine (the ``adblock`` package, MIT/Apache-2.0):

* **EasyList** and **EasyPrivacy** (easylist.to, GPLv3 / CC BY-SA 3.0),
* **uBlock Origin filters**, **badware risks** and **unbreak** (uBlockOrigin/uAssets, GPLv3) — the unbreak list
  holds the exceptions that keep sites working,
* **Peter Lowe's ad and tracking server list** (pgl.yoyo.org).

A copy of every list is bundled with the app; the newest ones are downloaded once a day (conditional requests)
and the engine is rebuilt in the background and swapped in. The engine is only used from the GUI thread (Qt
WebEngine calls the request interceptor there), the swap is a single reference assignment.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import adblock as _adblock
    AVAILABLE = True
except ImportError:                       # pragma: no cover - optional at runtime
    _adblock = None
    AVAILABLE = False


@dataclass(frozen=True)
class FilterList:
    key: str
    name: str
    url: str
    min_rules: int                         # fewer rules than this: truncated or not the list → not used


LISTS = (
    FilterList("easylist", "EasyList", "https://easylist.to/easylist/easylist.txt", 20_000),
    FilterList("easyprivacy", "EasyPrivacy", "https://easylist.to/easylist/easyprivacy.txt", 15_000),
    FilterList("ublock-filters", "uBlock filters", "https://ublockorigin.github.io/uAssets/filters/filters.txt",
               2_000),
    FilterList("ublock-badware", "uBlock badware risks",
               "https://ublockorigin.github.io/uAssets/filters/badware.txt", 1_000),
    FilterList("ublock-unbreak", "uBlock unbreak", "https://ublockorigin.github.io/uAssets/filters/unbreak.txt",
               500),
    FilterList("peter-lowe", "Peter Lowe's list",
               "https://pgl.yoyo.org/adservers/serverlist.php?hostformat=adblockplus&showintro=0&mimetype=plaintext",
               1_000),
)
BY_KEY = {fl.key: fl for fl in LISTS}
MAX_BYTES = 8 * 1024 * 1024

# never touched by the lists: the checks the user has to complete
_CHECKS = ("challenges.cloudflare.com", "hcaptcha.com", "recaptcha.net", "gstatic.com")
_CHECK_PATH = re.compile(r"^https://www\.google\.com/recaptcha/", re.I)

_LOCK = threading.Lock()
_ENGINE = None                              # adblock.Engine, replaced as a whole when lists change
_INFO: dict = {}
_DATA_DIR: Path | None = None

# Qt WebEngine resource types (QWebEngineUrlRequestInfo.ResourceType) → filter-list request types
QT_TYPES = {0: "document", 1: "sub_frame", 2: "stylesheet", 3: "script", 4: "image", 5: "font", 6: "other",
            7: "object", 8: "media", 9: "script", 10: "script", 11: "other", 12: "image", 13: "xmlhttprequest",
            14: "ping", 15: "script", 16: "csp_report", 17: "object", 19: "document", 20: "sub_frame",
            21: "xmlhttprequest", 254: "websocket", 255: "other"}


def set_data_dir(path: Path | None) -> None:
    global _DATA_DIR
    _DATA_DIR = Path(path) if path is not None else None


def _downloaded(key: str) -> Path | None:
    return _DATA_DIR / f"{key}.txt.gz" if _DATA_DIR else None


def rule_count(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln and not ln.startswith(("!", "[")))


def _read_list(fl: FilterList) -> tuple[str, str]:
    """(text, where from) — the downloaded copy when it is usable, else the bundled one."""
    p = _downloaded(fl.key)
    if p is not None and p.is_file():
        try:
            text = gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
            if rule_count(text) >= fl.min_rules:
                return text, "downloaded"
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            log.warning("filter list %s unreadable, using the bundled copy: %s", fl.key, exc)
    try:
        blob = resources.files("ezgd.resources").joinpath(f"adblock/filters/{fl.key}.txt.gz").read_bytes()
        return gzip.decompress(blob).decode("utf-8", "replace"), "bundled"
    except (OSError, FileNotFoundError, ModuleNotFoundError, EOFError, gzip.BadGzipFile):
        return "", "missing"


def build_engine():
    """Compile every list into one engine (≈0.2 s); returns (engine, info)."""
    if not AVAILABLE:
        return None, {"available": False}
    fs = _adblock.FilterSet(debug=False)
    lists, rules = {}, 0
    for fl in LISTS:
        text, origin = _read_list(fl)
        if text:
            fs.add_filter_list(text, format="standard")
            n = rule_count(text)
            rules += n
            lists[fl.key] = {"name": fl.name, "rules": n, "origin": origin}
    engine = _adblock.Engine(fs, optimize=True)
    return engine, {"available": True, "rules": rules, "lists": lists, "built_at": time.time()}


def engine():
    global _ENGINE, _INFO
    if _ENGINE is None and AVAILABLE:
        with _LOCK:
            if _ENGINE is None:
                t = time.monotonic()
                eng, info = build_engine()
                _INFO = info
                _ENGINE = eng
                log.info("filter lists: %d rules from %d lists (%.2f s)", info.get("rules", 0),
                         len(info.get("lists", {})), time.monotonic() - t)
    return _ENGINE


def reload() -> None:
    """Rebuild from the current files and swap the engine in (call from a worker thread)."""
    global _ENGINE, _INFO
    eng, info = build_engine()
    with _LOCK:
        _ENGINE, _INFO = eng, info


def info() -> dict:
    engine()
    return dict(_INFO) if _INFO else {"available": AVAILABLE}


def _is_check(url: str) -> bool:
    host = re.sub(r"^[a-z]+://([^/:?#]+).*$", r"\1", url.lower())
    return any(host == c or host.endswith("." + c) for c in _CHECKS) or bool(_CHECK_PATH.match(url))


def blocked(url: str, source_url: str, request_type: str) -> str | None:
    """The rule that blocks this request (``None`` when it may load)."""
    eng = engine()
    if eng is None or not url.startswith(("http://", "https://", "ws://", "wss://")) or _is_check(url):
        return None
    try:
        r = eng.check_network_urls(url, source_url or url, request_type)
    except Exception:                      # a malformed URL must never break page loading
        return None
    if r.matched and r.exception is None:
        return r.filter or "filter list"
    return None


def popup_blocked(url: str, opener_url: str) -> bool:
    """A new window / page to an ad: matched by the lists' ``$popup`` / ``$document`` rules."""
    return bool(blocked(url, opener_url, "popup") or blocked(url, opener_url, "document"))


def cosmetic(url: str) -> tuple[list[str], set[str], bool]:
    """(selectors to hide on this page, exceptions, generic hiding switched off for the page)."""
    eng = engine()
    if eng is None:
        return [], set(), True
    try:
        r = eng.url_cosmetic_resources(url)
    except Exception:
        return [], set(), True
    return list(r.hide_selectors), set(r.exceptions), bool(r.generichide)


def generic_selectors(classes: list[str], ids: list[str], exceptions: set[str]) -> list[str]:
    """Generic element-hiding rules (``##.ad-banner``) that apply to the classes/ids present on a page."""
    eng = engine()
    if eng is None or not (classes or ids):
        return []
    try:
        return list(eng.hidden_class_id_selectors(classes[:2000], ids[:2000], set(exceptions)))
    except Exception:
        return []


def install_list(key: str, text: str) -> int:
    """Validate a freshly downloaded list and store it; returns its rule count (the engine is not rebuilt)."""
    fl = BY_KEY[key]
    n = rule_count(text)
    if n < fl.min_rules:
        raise ValueError(f"{fl.name}: only {n} rules (expected at least {fl.min_rules}) — kept the current copy")
    if not re.search(r"(\|\||##|^@@)", text, re.M):
        raise ValueError(f"{fl.name}: the download is not a filter list — kept the current copy")
    p = _downloaded(key)
    if p is None:
        raise ValueError("no data folder for the filter lists")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        fh.write(text)
    os.replace(tmp, p)
    return n


# --------------------------------------------------------- in-page scripts
# element hiding: the page's classes and ids are reported (isolated world, so the page cannot interfere) and the
# matching generic rules come back as a style sheet; runs again for content added later, at most every second
COLLECT_JS = r"""
(function () {
  if (window.__ezgdCosm) return; window.__ezgdCosm = true;
  var seenC = Object.create(null), seenI = Object.create(null), rounds = 0, timer = 0;
  function scan() {
    timer = 0; if (++rounds > 40) return;
    var c = [], i = [], els = document.querySelectorAll('[class],[id]');
    for (var k = 0; k < els.length && k < 20000; k++) {
      var e = els[k];
      if (e.id && !seenI[e.id]) { seenI[e.id] = 1; i.push(e.id); }
      var cl = e.classList;
      if (cl) for (var j = 0; j < cl.length; j++) { var n = cl[j]; if (!seenC[n]) { seenC[n] = 1; c.push(n); } }
    }
    if (c.length || i.length) console.debug('__ezgd_cosm__ ' + JSON.stringify({c: c, i: i}));
  }
  function later() { if (!timer) timer = setTimeout(scan, 1000); }
  function start() {
    scan();
    try { new MutationObserver(later).observe(document.documentElement, {childList: true, subtree: true}); }
    catch (e) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
"""


def style_js(selectors: list[str], tag: str) -> str:
    """JavaScript that adds a style sheet hiding ``selectors`` (as soon as the document exists)."""
    import json
    css = "".join(f"{s}{{display:none!important}}\n" for s in selectors if "{" not in s and "}" not in s)
    return ("(function(){var css=%s;function add(){var r=document.documentElement;if(!r){return setTimeout(add,10);}"
            "var s=document.createElement('style');s.setAttribute('data-ezgd',%s);s.textContent=css;"
            "(document.head||r).appendChild(s);}add();})();" % (json.dumps(css), json.dumps(tag)))
