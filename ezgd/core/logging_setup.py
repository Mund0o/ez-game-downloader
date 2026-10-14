"""Rotating file + console logging with URL redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_QUERY_RE = re.compile(r"(https?://[^\s?#'\"]+)\?[^\s'\"]*")
_MAGNET_RE = re.compile(r"(magnet:\?xt=urn:[a-z0-9]+:[A-Za-z0-9]+)[^\s'\"]*", re.I)
_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@")


def redact(text: str) -> str:
    """Strip query strings, URL credentials and magnet trackers from log text.

    Query strings on file-host links frequently carry one-time tokens.
    """
    text = _USERINFO_RE.sub(r"\1***@", text)
    text = _QUERY_RE.sub(r"\1?…", text)
    text = _MAGNET_RE.sub(r"\1…", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "ezgd.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(RedactingFormatter(LOG_FORMAT))
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(RedactingFormatter(LOG_FORMAT))
        root.addHandler(ch)
    set_level(level)
    for noisy in ("httpx", "httpcore", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


def set_level(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))
