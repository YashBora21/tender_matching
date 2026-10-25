"""The fetch fence: the allowlist, the redirects, the private addresses, the
limits. These are the checks that keep a link in a document from reaching
anywhere it should not.
"""

from __future__ import annotations

import http.server
import socket
import threading

import pytest

from app.core import fetching
from app.core.fetching import FetchRefused, check_url, fetch


def _resolves_to(monkeypatch, *addresses: str) -> None:
    """Make a host resolve to the given addresses, without the network."""
    family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
    monkeypatch.setattr(
        "app.core.fetching.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (family, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))
            for address in addresses
        ],
    )



def test_a_host_off_the_allowlist_is_refused(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "gem.gov.in")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(FetchRefused) as caught:
        check_url("https://evil.example.com/a.pdf")
    assert "allowlist" in str(caught.value)


def test_a_host_on_the_allowlist_is_allowed(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "gem.gov.in")
    from app.core.config import get_settings

    get_settings.cache_clear()
    _resolves_to(monkeypatch, "160.187.232.19")

    check_url("https://mkp.gem.gov.in/a.pdf")


def test_a_subdomain_of_an_allowed_host_is_allowed(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "gem.gov.in")
    from app.core.config import get_settings

    get_settings.cache_clear()
    _resolves_to(monkeypatch, "160.187.232.19")

    check_url("https://bidplus.gem.gov.in/a.pdf")


def test_a_nat64_address_is_judged_by_the_ipv4_it_carries(monkeypatch):
    """On an IPv4-only network with DNS64, a public name resolves into
    64:ff9b::/96. Python classes that prefix as reserved, but the IPv4 inside
    it is public, so the document is reachable and must not be refused."""
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "gem.gov.in")
    from app.core.config import get_settings

    get_settings.cache_clear()
    # 64:ff9b::a0bb:e813 carries 160.187.232.19.
    _resolves_to(monkeypatch, "64:ff9b::a0bb:e813")

    check_url("https://mkp.gem.gov.in/a.pdf")


def test_a_nat64_address_that_carries_a_private_ipv4_is_refused(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "gem.gov.in")
    from app.core.config import get_settings

    get_settings.cache_clear()
    # 64:ff9b::c0a8:101 carries 192.168.1.1.
    _resolves_to(monkeypatch, "64:ff9b::c0a8:101")

    with pytest.raises(FetchRefused) as caught:
        check_url("https://mkp.gem.gov.in/a.pdf")
    assert "private address" in str(caught.value)



def test_a_private_address_is_refused(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "localhost")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(FetchRefused) as caught:
        check_url("http://localhost/a.pdf")
    assert "private address" in str(caught.value)


def test_a_loopback_address_is_refused(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "127.0.0.1")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(FetchRefused):
        check_url("http://127.0.0.1/a.pdf")


def test_a_scheme_that_is_not_http_is_refused(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "gem.gov.in")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(FetchRefused) as caught:
        check_url("file:///etc/passwd")
    assert "scheme" in str(caught.value)


def test_a_redirect_is_refused_not_followed(monkeypatch):
    """A redirect is a fresh URL, and a fresh URL is a fresh decision."""
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "127.0.0.1")
    monkeypatch.setenv("FETCH_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/elsewhere.pdf")
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with pytest.raises(FetchRefused):
            fetch(f"http://127.0.0.1:{httpd.server_address[1]}/a.pdf")
    finally:
        httpd.shutdown()


def test_a_document_over_the_size_limit_is_refused(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "127.0.0.1")
    monkeypatch.setenv("FETCH_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class Big(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Big)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with pytest.raises(FetchRefused) as caught:
            fetch(f"http://127.0.0.1:{httpd.server_address[1]}/a.pdf", max_bytes=1000)
        assert "limit" in str(caught.value)
    finally:
        httpd.shutdown()


def test_a_server_that_lies_about_its_size_cannot_make_the_collector_read_forever(monkeypatch):
    """A server that understates Content-Length cannot make the collector
    read past the limit: the read itself is capped."""
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "127.0.0.1")
    monkeypatch.setenv("FETCH_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class Liar(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(b"x" * 5000)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Liar)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        got = fetch(f"http://127.0.0.1:{httpd.server_address[1]}/a.pdf", max_bytes=1000)
        # Whatever the server claimed, the collector holds no more than the
        # limit allows.
        assert len(got.data) <= 1000
    finally:
        httpd.shutdown()


def test_a_document_is_fetched_within_the_limits(monkeypatch):
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "127.0.0.1")
    monkeypatch.setenv("FETCH_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    from app.core.config import get_settings

    get_settings.cache_clear()

    class Small(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b"%PDF-1.4 hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Small)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        got = fetch(f"http://127.0.0.1:{httpd.server_address[1]}/a.pdf")
        assert got.data == b"%PDF-1.4 hello"
    finally:
        httpd.shutdown()
