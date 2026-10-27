"""Shared crash knowledge with everyone who uses the app (server/knowledge — a Cloudflare Worker).

**Sending.** When this computer solves a failure, what fixed it goes to the server — anonymised first: the failure's
pattern without the game's title, build or registry keys, the fix as rebuilt by ``tracer.clean_record`` (only
known, harmless settings; text that names a file left out) and the GPU vendor. Nothing else about the computer or the
player. Each report is **sealed with a fresh random key** (an X25519 key made for this one report → HKDF-SHA256 →
AES-256-GCM) to the server's public key: only the server can open it — not the network, not a proxy, not other users.

**Receiving.** The server publishes failure pattern → fix → how many different networks it worked on, **signed**
with its Ed25519 key. The app checks the signature (anything else is thrown away), then cleans every entry again
(``tracer.clean_record``) — so neither a forged file nor a poisoned report can make this computer run anything: at
worst a useless setting is tried in a test copy of the game. A fix confirmed in more places counts for more.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

AAD = b"ezgd-crowd-v1"
INFO = b"ezgd crowd v1"
MAX_KNOWLEDGE = 8 * 1024 * 1024
_SIG_SHARED = ("engine", "code", "module", "null", "replaced", "loaders", "box", "missing")   # (no build, no keys)
_PLAIN_LABEL = re.compile(r"^[A-Za-z0-9 _.,:;()+\-&'%=]{1,120}$")


def _canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def seal(payload: dict, server_key_b64: str) -> dict:
    """``payload`` encrypted so that only the holder of ``server_key_b64``'s private half can read it — with a key
    made for this one report."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    server_raw = base64.b64decode(server_key_b64)
    eph = X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(server_raw))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + server_raw, info=INFO).derive(shared)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, _canonical(payload).encode("utf-8"), AAD)
    return {"v": 1, "epk": base64.b64encode(epk).decode(), "nonce": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(ct).decode()}


def _anonymous_fix(fx: dict) -> dict | None:
    from ezgd.library import tracer
    c = tracer._clean_fix(fx)
    if c is None:
        return None
    label = c.get("label") or ""
    c["label"] = label if _PLAIN_LABEL.match(label) and "home" not in label.lower() else ""
    c["why"] = ""                                       # (free text: may name the game or a folder)
    return c


def reports(solved: list[dict], machine: dict, sent: set[str]) -> tuple[list[dict], list[str]]:
    """This computer's solved failures as anonymous records not sent before: (records, their ids)."""
    from ezgd.library import tracer
    from ezgd.platform.machine import gpu_vendor
    out, ids = [], []
    vendor = gpu_vendor(machine) or ""
    for rec in solved:
        if (rec.get("machine") or {}).get("id") not in (None, "", machine.get("id")):
            continue                                    # (only what this computer solved)
        sig = {k: v for k, v in (rec.get("sig") or {}).items() if k in _SIG_SHARED}
        if not sig or not tracer.has_evidence(sig):
            continue                                    # (a fingerprint without evidence matches nothing)
        for fx in rec.get("fixes") or []:
            fix = _anonymous_fix(fx)
            if fix is None:
                continue
            rid = hashlib.sha256(_canonical([sig, fix]).encode()).hexdigest()[:24]
            if rid in sent or rid in ids:
                continue
            out.append({"sig": sig, "fix": fix, "machine": {"gpu": vendor if vendor in ("nvidia", "amd", "intel")
                                                            else "other"}})
            ids.append(rid)
    return out, ids


def verify(body: bytes, sign_key_b64: str) -> dict | None:
    """The server's published knowledge, or None unless its signature is valid."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not sign_key_b64 or len(body) > MAX_KNOWLEDGE:
        return None
    try:
        outer = json.loads(body)
        doc, sig = outer["doc"], base64.b64decode(outer["sig"], validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(sign_key_b64)).verify(sig, doc.encode("utf-8"))
        data = json.loads(doc)
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return None
    return data if isinstance(data, dict) and data.get("v") == 1 else None


def records_from(doc: dict) -> list[dict]:
    """Published entries as records for ``tracer.recall`` (cleaned there again)."""
    out = []
    for e in (doc.get("entries") or [])[:5000] if isinstance(doc.get("entries"), list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("fix"), dict):
            continue
        vendors = e.get("vendors") if isinstance(e.get("vendors"), dict) else {}
        total = sum(v for v in vendors.values() if isinstance(v, int)) or 1
        top = max(vendors, key=lambda k: vendors[k] if isinstance(vendors[k], int) else 0) if vendors else ""
        only = top if top in ("nvidia", "amd", "intel") and vendors.get(top, 0) == total else ""
        fx = e["fix"]
        out.append({"sig": e.get("sig"), "game": "", "fixes": [fx],
                    "labels": [fx.get("label") or fx.get("gen_id") or ""], "at": e.get("last") or 0,
                    "machine": {"gpus": [{"vendor": only}]} if only else {}, "n": e.get("n") or 1})
    return out


# ------------------------------------------------------------------ what was sent (kept on this computer)
def load_sent(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_sent(path: Path, ids: set[str]) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(sorted(ids)[-5000:]), "utf-8")
    os.replace(tmp, path)
