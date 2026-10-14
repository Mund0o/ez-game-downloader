"""Refresh the ad blocklist bundled with the app (run by the build scripts; safe to run any time).

Downloads StevenBlack/hosts (MIT licence), validates it and rewrites ezgd/resources/adblock/. If the download
fails or looks wrong, the bundled list is left as it is and the build continues.
"""

from __future__ import annotations

import gzip
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ezgd.net import adblock  # noqa: E402

OUT = ROOT / "ezgd" / "resources" / "adblock"
LICENSE_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/license.txt"


def fetch(url: str, limit: int = 16 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "EZGameDownloader-build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response too large")
    return data


def main() -> int:
    try:
        text = fetch(adblock.SOURCE_URL).decode("utf-8", "replace")
        domains, date = adblock.parse_hosts(text)
        if len(domains) < adblock.MIN_DOMAINS:
            raise ValueError(f"only {len(domains)} domains")
        lic = fetch(LICENSE_URL, 64 * 1024)
    except Exception as exc:                       # the build must not fail because of this
        print(f"ad blocklist not refreshed ({exc}); keeping the bundled list")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    header = (f"# StevenBlack/hosts (unified adware + malware), MIT licence — {adblock.SOURCE_URL}\n"
              f"# Date: {date or 'unknown'}\n")
    with gzip.open(OUT / adblock.LIST_FILE, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(header + "\n".join(sorted(domains)) + "\n")
    (OUT / "LICENSE-StevenBlack-hosts.txt").write_bytes(lic)
    print(f"ad blocklist refreshed: {len(domains)} domains, list date {date}")
    refresh_filter_lists()
    return 0


def refresh_filter_lists() -> None:
    """EasyList, EasyPrivacy, uBlock filters/badware/unbreak, Peter Lowe — each kept as it is if its download fails."""
    from ezgd.net import filterlists
    out = OUT / "filters"
    out.mkdir(parents=True, exist_ok=True)
    for fl in filterlists.LISTS:
        try:
            text = fetch(fl.url, filterlists.MAX_BYTES).decode("utf-8", "replace")
            n = filterlists.rule_count(text)
            if n < fl.min_rules:
                raise ValueError(f"only {n} rules")
        except Exception as exc:                   # never fail the build
            print(f"{fl.name} not refreshed ({exc}); keeping the bundled copy")
            continue
        with gzip.open(out / f"{fl.key}.txt.gz", "wt", encoding="utf-8", compresslevel=9) as fh:
            fh.write(text)
        print(f"{fl.name} refreshed: {n} rules")


if __name__ == "__main__":
    raise SystemExit(main())
