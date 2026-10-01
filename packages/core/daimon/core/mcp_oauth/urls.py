"""Which URLs daimon will contact on a model's or a remote server's say-so.

Before this flow nothing in daimon fetched an MCP URL itself; Managed Agents
did, from its own network. The probe and discovery now run inside the
deployment, so a URL a prompt-injected agent or a hostile server names must
be a public https endpoint. Names are not resolved here: a hostname that
points at a private range is the egress policy's problem, an address that
already is one is refused up front.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from daimon.core.errors import DaimonError

_LOCAL_SUFFIXES = (".localhost", ".local", ".internal")


class McpUrlError(DaimonError):
    """The URL is not a public endpoint daimon will contact."""


def assert_public_host(url: str, *, what: str = "url") -> str:
    """Refuse a URL with no host, embedded credentials, a local name or a non-public address."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        raise McpUrlError(f"{what} must name a host: {url}")
    if parsed.username is not None or parsed.password is not None:
        raise McpUrlError(f"{what} must not carry credentials: {url}")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise McpUrlError(f"{what} points at a local name: {url}")
    address = _literal_address(host)
    if address is not None and not address.is_global:
        raise McpUrlError(f"{what} points at a non-public address: {url}")
    return url


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a literal host denotes, including `127.1`, decimal and hex IPv4 forms."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        # inet_aton accepts every shorthand the resolver would: no DNS involved.
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except OSError:
        return None


def assert_public_https_url(url: str, *, what: str = "url") -> str:
    """`assert_public_host` plus the https scheme; every URL daimon fetches passes here."""
    if urlparse(url).scheme != "https":
        raise McpUrlError(f"{what} must be https: {url}")
    return assert_public_host(url, what=what)
