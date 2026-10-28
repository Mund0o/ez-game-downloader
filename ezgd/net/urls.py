"""URL and destination validation (SSRF protection)."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

MAX_URL_LENGTH = 4096
ALLOWED_SCHEMES = frozenset({"http", "https"})
_LEGACY_IPV4 = re.compile(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}", re.I)


class UnsafeURLError(ValueError):
    """The URL is malformed or points somewhere the app must not connect to."""


class LookupFailed(OSError):
    """The site's address could not be found (DNS). Often only for a moment — the connection tries again."""


def is_public_ip(addr: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return is_public_ip(str(ip.ipv4_mapped))
        if ip.sixtofour:
            return is_public_ip(str(ip.sixtofour))
        if ip.teredo:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def validate_url(url: str, *, allow_private: bool = False,
                 schemes: frozenset[str] = ALLOWED_SCHEMES) -> str:
    """Syntactic checks plus literal-IP checks. Returns the URL unchanged.

    Host names are resolved and checked again at connection time by
    :class:`ezgd.net.client.GuardedBackend`, which also defeats DNS rebinding.
    """
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("empty URL")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("URL is too long")
    if any(ch in url for ch in "\r\n\t\x00 "):
        raise UnsafeURLError("URL contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL: {exc}") from exc
    if parts.scheme.lower() not in schemes:
        raise UnsafeURLError(f"unsupported URL scheme '{parts.scheme}'")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not accepted")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    if port is not None and not (0 < port < 65536):
        raise UnsafeURLError("invalid port")
    if not allow_private:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
            raise UnsafeURLError(f"local host names are not allowed: {host}")
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            # Legacy IPv4 spellings ("2130706433", "0x7f.1", "127.1") are resolved
            # as addresses by many resolvers; never accept them as host names.
            if _LEGACY_IPV4.fullmatch(host):
                raise UnsafeURLError(f"numeric host not allowed: {host}")
        else:
            if not is_public_ip(host.strip("[]")):
                raise UnsafeURLError(f"address {host} is not a public internet address")
    return url


def resolve_redirect(base: str, location: str) -> str:
    return urljoin(base, location.strip())


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


async def resolve_public(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """Resolve ``host`` and return addresses; raise if any is non-public.

    Rejecting the whole name when *any* record is private stops mixed
    public/private answers from being used to reach the local network.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LookupFailed(f"{host} could not be found ({exc.strerror or exc})") from exc
    addrs = []
    for family, _t, _p, _c, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        addr = sockaddr[0]
        if not allow_private and not is_public_ip(addr):
            raise UnsafeURLError(f"{host} resolves to non-public address {addr}")
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise LookupFailed(f"{host} could not be found (no usable address)")
    return addrs
