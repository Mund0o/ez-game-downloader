import httpx
import pytest

from ezgd.net.client import SafeHttpClient
from ezgd.net.urls import UnsafeURLError, is_public_ip, resolve_public, validate_url


@pytest.mark.parametrize("url", [
    "ftp://example.com/file", "file:///etc/passwd", "javascript:alert(1)", "",
    "http://user:pass@example.com/", "http://127.0.0.1/", "http://10.0.0.5/x", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://localhost:8080/", "http://printer.local/", "http://2130706433/", "http://0x7f.1/",
    "http://127.1/", "http://exa mple.com/", "http://example.com:99999/", "http://0.0.0.0/",
    "http://[fe80::1]/", "http://100.64.0.1/",
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


@pytest.mark.parametrize("url", [
    "https://online-fix.me/page/2/", "https://uploads.online-fix.me:2053/torrents/PEAK/",
    "https://steamrip.com/wp-json/wp/v2/posts?search=a", "http://8.8.8.8/", "https://[2606:4700::1]/",
])
def test_public_urls_accepted(url):
    assert validate_url(url) == url


def test_is_public_ip():
    assert is_public_ip("1.1.1.1")
    assert not is_public_ip("127.0.0.1")
    assert not is_public_ip("::ffff:10.0.0.1")
    assert not is_public_ip("not-an-ip")


async def test_resolve_public_blocks_loopback_names():
    with pytest.raises(UnsafeURLError):
        await resolve_public("localhost", 80)


async def test_guarded_backend_refuses_private_connection(server):
    server.add("/x", data=b"secret")
    client = SafeHttpClient(timeout=5)       # allow_private = False
    try:
        with pytest.raises(UnsafeURLError):
            await client.fetch(server.url("/x"))
    finally:
        await client.aclose()


async def test_redirect_to_private_address_blocked():
    def handler(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/admin"})
    client = SafeHttpClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UnsafeURLError):
            await client.fetch("https://example.org/start")
    finally:
        await client.aclose()


async def test_https_downgrade_blocked():
    def handler(req):
        return httpx.Response(301, headers={"location": "http://example.org/plain"})
    client = SafeHttpClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UnsafeURLError, match="HTTPS"):
            await client.fetch("https://example.org/start")
    finally:
        await client.aclose()


async def test_redirect_limit():
    def handler(req):
        n = int(req.url.params.get("n", 0))
        return httpx.Response(302, headers={"location": f"https://example.org/r?n={n + 1}"})
    client = SafeHttpClient(transport=httpx.MockTransport(handler), max_redirects=3)
    try:
        with pytest.raises(UnsafeURLError, match="too many redirects"):
            await client.fetch("https://example.org/r?n=0")
    finally:
        await client.aclose()


async def test_redirects_followed_and_referer_dropped_cross_host():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("referer")))
        if req.url.host == "a.example.org":
            return httpx.Response(302, headers={"location": "https://b.example.org/file"})
        return httpx.Response(200, content=b"ok")
    client = SafeHttpClient(transport=httpx.MockTransport(handler))
    try:
        res = await client.fetch("https://a.example.org/start", headers={"Referer": "https://a.example.org/"})
    finally:
        await client.aclose()
    assert res.content == b"ok" and res.url == "https://b.example.org/file"
    assert seen[1][1] is None


async def test_response_size_cap():
    client = SafeHttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000)))
    try:
        from ezgd.net.client import ResponseTooLarge
        with pytest.raises(ResponseTooLarge):
            await client.fetch("https://example.org/big", max_bytes=1000)
    finally:
        await client.aclose()


def test_tls_context_includes_gen_y_roots():
    from ezgd.net.tls import ssl_context
    subjects = [dict(x[0] for x in c["subject"]).get("commonName") for c in ssl_context().get_ca_certs()]
    assert "Root YE" in subjects and "Root YR" in subjects
