"""Sign a release file, so installed copies only install updates made by the holder of the release key.

The SHA-256 GitHub publishes next to a release only proves the download is the file on the release page — not who
put it there. Whoever got hold of the GitHub account (or a token) could publish a "new version" that every copy
installs. So each release file gets an Ed25519 signature over its SHA-256, version and name, made with a private key
that never leaves this computer (and is never in the repository); the app carries the public key
(ezgd/core/release_keys.py) and installs nothing without a valid signature.

    .venv/bin/python packaging/sign_release.py --init            # once: make the key, print the public key
    .venv/bin/python packaging/sign_release.py FILE VERSION      # writes FILE.sig (publish_github.sh does this)

Back the key file up (a password manager, a USB stick): without it no further update can be signed, and every copy
would have to be updated by hand once to a version with a new public key.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ezgd.core.release_keys import message  # noqa: E402

KEY = Path(os.environ.get("EZGD_RELEASE_KEY") or
           Path.home() / ".local" / "share" / "ezgd-release-signing" / "ed25519.key")


def _public_b64(priv) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def load():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(KEY.read_text().strip()))


def init() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if KEY.exists():
        print(f"{KEY} already exists — public key: {_public_b64(load())}")
        return
    KEY.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY.parent, 0o700)
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(base64.b64encode(raw).decode() + "\n")
    print(f"made {KEY} (back it up!) — public key for ezgd/core/release_keys.py: {_public_b64(priv)}")


def sign(path: Path, version: str) -> Path:
    from ezgd.core.release_keys import PUBLIC_KEYS
    priv = load()
    if _public_b64(priv) not in PUBLIC_KEYS:
        sys.exit("This key's public half is not in ezgd/core/release_keys.py — installed copies would refuse it.")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    sig = priv.sign(message(path.name, version, sha))
    out = path.with_name(path.name + ".sig")
    out.write_text(base64.b64encode(sig).decode() + "\n")
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["--init"]:
        init()
    elif len(sys.argv) == 3:
        print(sign(Path(sys.argv[1]), sys.argv[2]))
    else:
        sys.exit(__doc__)
