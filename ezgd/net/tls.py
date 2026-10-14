"""TLS trust: certifi + bundled Let's Encrypt Gen-Y roots.

online-fix.me serves a chain ending in "ISRG Root YE" (Let's Encrypt's 2025+
hierarchy) without a cross-signed intermediate. That root is not yet present
in certifi 2026.07.22 or common OS stores, so verification would fail. Instead
of disabling verification we add Let's Encrypt's official roots (fetched from
letsencrypt.org/certificates and authenticated via their cross-signature by
the already-trusted ISRG Root X2). Verification stays fully enabled.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from importlib import resources

import certifi

EXTRA_ROOTS = ("isrg-root-ye.pem", "isrg-root-yr.pem")


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    certs = resources.files("ezgd.resources").joinpath("certs")
    for name in EXTRA_ROOTS:
        pem = certs.joinpath(name)
        if pem.is_file():
            ctx.load_verify_locations(cadata=pem.read_text("ascii"))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
