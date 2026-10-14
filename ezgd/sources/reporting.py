"""User-initiated problem reports for unsupported sources or malformed listings.

Reports are plain-text files saved locally (nothing is uploaded). They contain
only what is needed to reproduce a parser problem: no cookies, no paths
outside the app, no query strings.
"""

from __future__ import annotations

import platform
import time
from pathlib import Path

from ezgd import __version__
from ezgd.core.logging_setup import redact
from ezgd.sources.base import PARSER_VERSION


def write_report(report_dir: Path, *, source: str, kind: str, url: str | None, message: str,
                 user_comment: str = "", status: str | None = None) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"report-{source}-{stamp}.txt"
    lines = [
        "EZ Game Downloader problem report",
        f"created: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"app version: {__version__} (parser v{PARSER_VERSION})",
        f"os: {platform.system()} {platform.release()} / Python {platform.python_version()}",
        f"source: {source}",
        f"problem: {kind}",
        f"source status: {status or 'unknown'}",
        f"url: {redact(url) if url else '-'}",
        "",
        "details:",
        redact(message or "-"),
        "",
        "user comment:",
        user_comment.strip() or "-",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
