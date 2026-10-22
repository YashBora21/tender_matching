"""Fetching a document from a link a tender carries.

A tender PDF links to documents on other hosts, and the collector follows
those links. That makes the collector a fetcher of URLs it did not choose,
so the fetches are fenced:

- only the hosts on the allowlist are fetched at all;
- a redirect to another host is refused, not followed;
- a host that resolves to a private address is refused, so a link cannot
  reach the machine's own network;
- the size and the time are bounded, so a slow or huge document cannot hang
  the run.

A refused fetch is a failed fetch. It is recorded as one, never as a
document that says nothing.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

USER_AGENT = "tender-corpus-collector/1.0"


class FetchRefused(Exception):
    """A fetch that was not allowed to happen. The reason is the message."""


@dataclass
class Fetched:
    """A document that was fetched."""

    url: str
    data: bytes
    content_type: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    # True when the server said the document has not changed.
    not_modified: bool = False


def allowed_domains() -> list[str]:
    """The hosts a document may be fetched from."""
    return [
        domain.strip().lower()
        for domain in get_settings().fetch_allowed_domains.split(",")
        if domain.strip()
    ]


def _host_is_allowed(host: str, domains: list[str]) -> bool:
    host = host.lower()
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _resolves_to_private(host: str) -> bool:
    """True when the host resolves to an address on the machine's own network.

    A link in a document is not trusted, and "http://127.0.0.1/admin" or
    "http://169.254.169.254/latest/meta-data" is a document too. The address
    is checked, not the name, because a name can say anything.
    """
    exceptions = [
        host.strip().lower()
        for host in get_settings().fetch_allowed_private_hosts.split(",")
        if host.strip()
    ]
    if host.lower() in exceptions:
        return False

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchRefused(f"the host '{host}' does not resolve: {exc}") from exc

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def check_url(url: str) -> None:
    """Refuse a URL that must not be fetched. Raises with the reason."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchRefused(f"'{parts.scheme}' is not a scheme that can be fetched")
    host = parts.hostname
    if not host:
        raise FetchRefused("the URL names no host")

    domains = allowed_domains()
    if not _host_is_allowed(host, domains):
        raise FetchRefused(
            f"'{host}' is not on the allowlist ({', '.join(domains) or 'none'})"
        )
    if _resolves_to_private(host):
        raise FetchRefused(f"'{host}' resolves to a private address")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is refused rather than followed.

    A redirect is a fresh URL, and a fresh URL is a fresh decision: it has
    to pass the allowlist and the private-address check on its own. The
    simplest way to hold that line is not to follow redirects at all.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise FetchRefused(f"the server redirected to '{newurl}', which was not followed")


def fetch(
    url: str,
    *,
    max_bytes: Optional[int] = None,
    timeout: Optional[int] = None,
    headers: Optional[dict] = None,
) -> Fetched:
    """Fetch a document, within the limits the configuration sets."""
    settings = get_settings()
    max_bytes = max_bytes or settings.fetch_max_bytes
    timeout = timeout or settings.fetch_timeout_seconds

    check_url(url)

    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchRefused(
                    f"the document is {int(declared)} bytes, over the {max_bytes} limit"
                )
            # The size is capped as it is read: a server that lies about
            # Content-Length cannot make the collector read forever.
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchRefused(f"the document is over the {max_bytes} byte limit")
            if declared and declared.isdigit() and int(declared) != len(data):
                raise FetchRefused(
                    f"the server declared {declared} bytes and sent {len(data)}"
                )
            return Fetched(
                url=url,
                data=data,
                content_type=response.headers.get("Content-Type"),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return Fetched(url=url, data=b"", not_modified=True)
        raise FetchRefused(f"the server returned HTTP {exc.code}") from exc
    except FetchRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised past
        raise FetchRefused(f"{type(exc).__name__}: {str(exc)[:120]}") from exc


def robots_allows(url: str) -> tuple[bool, Optional[str]]:
    """Whether robots.txt lets the collector fetch this URL.

    A robots.txt that cannot be read is not a restriction: a server error is
    a broken site, not a policy. A 401 or 403 *is* a policy, and is treated
    as disallow-all.
    """
    parts = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"robots.txt is not readable (HTTP {exc.code}); treated as disallow-all"
        if 400 <= exc.code < 500:
            return True, None
        return True, f"robots.txt could not be read (HTTP {exc.code}); no restriction applied"
    except Exception as exc:  # noqa: BLE001
        return True, f"robots.txt could not be fetched ({type(exc).__name__}); no restriction applied"

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    if parser.can_fetch(USER_AGENT, url):
        return True, None
    return False, "robots.txt disallows this path for the collector's user agent"
