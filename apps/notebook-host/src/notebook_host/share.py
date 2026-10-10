"""Notebook-scoped share links that keep marimo's access token on the host."""

from __future__ import annotations

import hashlib
import hmac


def share_key(slug: str, access_token: str) -> str:
    """A stable capability for this notebook, revoked when its token changes."""
    return hmac.new(
        access_token.encode(), f"daimon-notebook-share\0{slug}".encode(), hashlib.sha256
    ).hexdigest()


def valid_share_key(slug: str, access_token: str, provided: str) -> bool:
    return bool(access_token) and hmac.compare_digest(
        share_key(slug, access_token).encode(), provided.encode()
    )
