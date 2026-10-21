"""Make the knowledge server's keys (once).

* KEY_X25519 — reports are sealed to its public half: only the worker can open them.
* KEY_ED25519 — the worker signs what it publishes with it: apps reject anything else.
* SALT — the worker hashes networks with it (to count confirmations from different places without keeping IPs).

The private keys go to Cloudflare as secrets (`wrangler secret put`) and into a backup file only you can read
(~/.local/share/ezgd-crowd-keys/secrets.json) — never into the repository. The public halves are written into
ezgd/core/crowd_keys.py, which ships with the app.

    ../../.venv/bin/python make_keys.py                 # make them (refuses to overwrite)
    ../../.venv/bin/python make_keys.py --put           # hand the secrets to wrangler (after `wrangler login`)
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = Path.home() / ".local" / "share" / "ezgd-crowd-keys" / "secrets.json"
KEYS_PY = HERE.parent.parent / "ezgd" / "core" / "crowd_keys.py"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make() -> dict:
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    raw = (s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())
    pub = (s.Encoding.Raw, s.PublicFormat.Raw)
    x, e = X25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    return {"KEY_X25519": json.dumps({"kty": "OKP", "crv": "X25519", "d": b64u(x.private_bytes(*raw)),
                                      "x": b64u(x.public_key().public_bytes(*pub))}),
            "KEY_ED25519": json.dumps({"kty": "OKP", "crv": "Ed25519", "d": b64u(e.private_bytes(*raw)),
                                       "x": b64u(e.public_key().public_bytes(*pub))}),
            "SALT": secrets.token_hex(32)}


def publics(sec: dict) -> tuple[str, str]:
    def std(u: str) -> str:
        return base64.b64encode(base64.urlsafe_b64decode(u + "=" * (-len(u) % 4))).decode()
    return std(json.loads(sec["KEY_X25519"])["x"]), std(json.loads(sec["KEY_ED25519"])["x"])


def write_public(sec: dict) -> None:
    enc, sign = publics(sec)
    text = KEYS_PY.read_text()
    text = re.sub(r'^ENCRYPT_KEY = ".*"$', f'ENCRYPT_KEY = "{enc}"', text, flags=re.M)
    text = re.sub(r'^SIGN_KEY = ".*"$', f'SIGN_KEY = "{sign}"', text, flags=re.M)
    KEYS_PY.write_text(text)


def main() -> None:
    if sys.argv[1:] == ["--put"]:
        sec = json.loads(STORE.read_text())
        for name in ("KEY_X25519", "KEY_ED25519", "SALT"):
            subprocess.run(["npx", "wrangler", "secret", "put", name], input=sec[name].encode(), cwd=HERE, check=True)
        print("secrets set on Cloudflare")
        return
    if STORE.exists():
        sys.exit(f"{STORE} already exists — the app's public keys must match it; not overwriting.")
    sec = make()
    STORE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE.parent, 0o700)
    fd = os.open(STORE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(sec, fh, indent=1)
    write_public(sec)
    print(f"made {STORE} (back it up) and wrote the public keys into {KEYS_PY.relative_to(HERE.parent.parent)}")


if __name__ == "__main__":
    main()
