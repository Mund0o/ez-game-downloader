"""SSRF-hardened async HTTP client.

* every URL (including every redirect hop) is validated before use;
* DNS is resolved by :class:`GuardedBackend`, which refuses non-public
  addresses and then connects to the vetted IP itself, so a second, different
  DNS answer (rebinding) cannot sneak a private address in;
* TLS is verified against the original host name (SNI is unaffected);
* redirects are followed manually with a hop limit and HTTPS→HTTP downgrade
  protection; environment proxies are ignored.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpcore
import httpx

from ezgd import __version__
from ezgd.net.ratelimit import HostRateLimiter
from ezgd.net.tls import ssl_context
from ezgd.net.urls import LookupFailed, UnsafeURLError, host_of, resolve_public, resolve_redirect, validate_url

log = logging.getLogger(__name__)

# A browser-like UA: both sites serve their normal public pages to it. The app
# identifies itself in the comment so site operators can recognise it.
DEFAULT_USER_AGENT = (f"Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0 "
                      f"EZGameDownloader/{__version__}")
REDIRECT_CODES = {301, 302, 303, 307, 308}


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Connects only to vetted addresses. A connection that failed at once — the site's address not found, or
    refused / unreachable — is tried again after RETRY_DELAYS: a DNS hiccup of a moment showed "could not load the
    page" until the player pressed Try again (a friend, cracked-games.org, 2026-10-28). A try that took long to fail
    (a timeout) is not repeated: that wait was long enough already. Nothing has been sent before a connection
    exists, so this is safe for every request."""

    RETRY_DELAYS = (1.0, 3.0)      # seconds before the second and the third try
    QUICK_FAILURE = 5.0            # a try that failed after longer than this is not repeated

    def __init__(self, allow_private: bool = False):
        self.allow_private = allow_private
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        tries = 0
        while True:
            tries += 1
            began = time.monotonic()
            try:
                stream = await self._connect_once(host, port, timeout, local_address, socket_options)
            except (LookupFailed, httpcore.ConnectError) as exc:
                if tries > len(self.RETRY_DELAYS) or time.monotonic() - began > self.QUICK_FAILURE:
                    raise
                delay = self.RETRY_DELAYS[tries - 1]
                log.debug("%s: %s — trying again in %g s", host, exc or type(exc).__name__, delay)
                await self.sleep(delay)
                continue
            if tries > 1:
                log.info("%s reached on try %d", host, tries)
            return stream

    async def _connect_once(self, host, port, timeout, local_address, socket_options):
        addrs = await resolve_public(host, port, allow_private=self.allow_private)
        last_exc: Exception | None = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(addr, port, timeout=timeout,
                                                     local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise UnsafeURLError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, allow_private: bool = False, max_connections: int = 32):
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=8,
                              keepalive_expiry=30)
        super().__init__(verify=ssl_context(), trust_env=False, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True, http2=False, retries=0,
            network_backend=GuardedBackend(allow_private),
        )


class HttpError(Exception):
    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class ResponseTooLarge(HttpError):
    pass


@dataclass
class FetchResult:
    url: str                    # final URL after redirects
    status: int
    headers: httpx.Headers
    content: bytes

    @property
    def encoding(self) -> str | None:
        ct = self.headers.get("content-type", "")
        for part in ct.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                return v.strip("\"' ").lower()
        return None

    def text(self, fallback: str = "utf-8") -> str:
        enc = self.encoding or fallback
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode(fallback, errors="replace")


class SafeHttpClient:
    def __init__(self, *, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 30.0,
                 allow_private: bool = False, max_redirects: int = 5,
                 rate_limiter: HostRateLimiter | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.allow_private = allow_private
        self.max_redirects = max_redirects
        self.rate_limiter = rate_limiter
        self._client = httpx.AsyncClient(
            transport=transport or GuardedTransport(allow_private),
            timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.8,ru;q=0.5"},
            follow_redirects=False, trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _open(self, method: str, url: str, headers: Mapping[str, str] | None,
                    polite: bool, content: bytes | None = None) -> httpx.Response:
        current = url
        hdrs = dict(headers or {})
        for _hop in range(self.max_redirects + 1):
            validate_url(current, allow_private=self.allow_private)
            host = host_of(current)
            if polite and self.rate_limiter:
                await self.rate_limiter.acquire(host)
            request = self._client.build_request(method, current, headers=hdrs, content=content)
            response = await self._client.send(request, stream=True)
            if response.status_code in REDIRECT_CODES and response.headers.get("location"):
                await response.aclose()
                nxt = resolve_redirect(current, response.headers["location"])
                if urlsplit(current).scheme == "https" and urlsplit(nxt).scheme == "http":
                    raise UnsafeURLError(f"refusing HTTPS→HTTP redirect to {host_of(nxt)}")
                if response.status_code == 303:
                    method, content = "GET", None
                if host_of(nxt) != host:
                    hdrs.pop("Referer", None)      # do not leak page URLs across hosts
                log.debug("redirect %s → %s", current, nxt)
                current = nxt
                continue
            return response
        raise UnsafeURLError(f"too many redirects (more than {self.max_redirects})")

    async def fetch(self, url: str, *, headers: Mapping[str, str] | None = None,
                    max_bytes: int = 8 * 1024 * 1024, polite: bool = True,
                    method: str = "GET", content: bytes | None = None) -> FetchResult:
        """Fetch a whole (bounded) response body. Does not raise for HTTP status codes."""
        response = await self._open(method, url, headers, polite, content)
        try:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLarge(f"response too large ({declared} bytes)", url=url)
            chunks, total = [], 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLarge(f"response exceeds {max_bytes} bytes", url=url)
                chunks.append(chunk)
            return FetchResult(str(response.url), response.status_code, response.headers, b"".join(chunks))
        finally:
            await response.aclose()

    @asynccontextmanager
    async def stream(self, url: str, *, headers: Mapping[str, str] | None = None,
                     polite: bool = False, method: str = "GET") -> AsyncIterator[httpx.Response]:
        response = await self._open(method, url, headers, polite)
        try:
            yield response
        finally:
            await response.aclose()
