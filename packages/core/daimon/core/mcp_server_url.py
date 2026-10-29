"""One comparison form for MCP server URLs, so every check agrees on "same server"."""

from __future__ import annotations

import posixpath
import uuid
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_mcp_url(url: str) -> str:
    """Lowercase scheme and host, drop a default port, the fragment and a trailing slash.

    The path and query keep their case: servers may treat them as significant.
    A value that does not parse as a URL only loses its trailing slash.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url.strip().rstrip("/")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    if parts.username or parts.password:
        userinfo = parts.username or ""
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        host = f"{userinfo}@{host}"
    return urlunsplit((scheme, host, parts.path.rstrip("/"), parts.query, ""))


def same_mcp_url(left: str, right: str) -> bool:
    return canonical_mcp_url(left) == canonical_mcp_url(right)


def ascii_mcp_url(url: str) -> str:
    """`canonical_mcp_url` with an internationalised host in its punycode form.

    Two spellings of one host compare equal. A host that will not encode is
    kept as it is, so it matches nothing but itself.
    """
    canonical = canonical_mcp_url(url)
    try:
        parts = urlsplit(canonical)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return canonical
    if not host or host.isascii() or parts.username or parts.password:
        return canonical
    try:
        encoded = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return canonical
    netloc = encoded if port is None else f"{encoded}:{port}"
    return urlunsplit(parts._replace(netloc=netloc))


def same_ascii_mcp_url(left: str, right: str) -> bool:
    return ascii_mcp_url(left) == ascii_mcp_url(right)


def session_mcp_url(public_url: str, session_id: str) -> str:
    """A distinct credential slot for one server-minted provider session grant."""
    parts = urlsplit(public_url)
    query = [*parse_qsl(parts.query, keep_blank_values=True), ("daimon_session", session_id)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def is_session_mcp_url(candidate: str, public_url: str) -> bool:
    """Only the real endpoint with one well-formed session selector is trusted.

    The selector conveys no authority; the mounted isolated vault supplies the
    signed credential. Other sessions never mount that vault.
    """
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    query = parse_qsl(parts.query, keep_blank_values=True)
    selectors = [value for key, value in query if key == "daimon_session"]
    if len(selectors) != 1:
        return False
    try:
        uuid.UUID(selectors[0])
    except ValueError:
        return False
    base = urlunsplit(
        parts._replace(query=urlencode([(k, v) for k, v in query if k != "daimon_session"]))
    )
    return same_mcp_url(base, public_url)


def same_mcp_endpoint(candidate: str, public_url: str) -> bool:
    """Conservative exclusion comparison, never a credential-trust predicate.

    Query selectors do not change the HTTP endpoint. Normalize path aliases as
    a proxy/router may, so renamed management servers cannot evade exclusion.
    """

    def endpoint(value: str) -> tuple[str, str, int | None, str]:
        parts = urlsplit(value.strip())
        path = parts.path
        for _ in range(3):
            path = unquote(path)
        path = "/".join(part.split(";", 1)[0] for part in path.replace("\\", "/").split("/"))
        path = posixpath.normpath("/" + path.lstrip("/")).casefold()
        return (
            parts.scheme.lower(),
            (parts.hostname or "").lower().rstrip("."),
            parts.port or _DEFAULT_PORTS.get(parts.scheme.lower()),
            path,
        )

    try:
        return endpoint(candidate) == endpoint(public_url)
    except ValueError:
        return False
