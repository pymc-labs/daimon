"""One comparison form for MCP server URLs, so every check agrees on "same server"."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

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
