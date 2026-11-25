"""The app's SQLAlchemy URL in the form psycopg expects."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SSL_MODES = {"true": "require", "false": "disable"}


def libpq_dsn(url: str) -> str:
    """`postgresql+asyncpg://...?ssl=true` as `postgresql://...?sslmode=require`."""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    query = [
        ("sslmode", _SSL_MODES.get(v, v)) if k == "ssl" else (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
