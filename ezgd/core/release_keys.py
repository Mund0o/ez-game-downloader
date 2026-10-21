"""The public half of the key app releases are signed with (packaging/sign_release.py).

An update is installed only when its ``<file>.sig`` verifies against one of these keys — over the file's SHA-256,
its version and its name, so a signed file cannot be passed off as another version or platform. The private key
stays on the developer's computer; a stolen GitHub account cannot make a valid signature.
"""

from __future__ import annotations

import base64

PUBLIC_KEYS: tuple[str, ...] = ("/wCVmkqdETAQEw8yIClwPg5F1nNjVk/QF0KNyMoOWu0=",)    # made 2026-10-21


def message(asset: str, version: str, sha256: str) -> bytes:
    return f"ezgd-release v1\n{asset}\n{version}\n{sha256.lower()}\n".encode()


def verify(signature_b64: str, asset: str, version: str, sha256: str, keys: tuple[str, ...] | None = None) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        sig = base64.b64decode(signature_b64.strip(), validate=True)
    except ValueError:
        return False
    for k in (PUBLIC_KEYS if keys is None else keys):
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(k)).verify(sig, message(asset, version, sha256))
            return True
        except (InvalidSignature, ValueError):
            continue
    return False
