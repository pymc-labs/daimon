"""Pure orchestration: mint slug, delegate to host_client.

Lives in core so it can be unit-tested without an MCP Context.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any, cast

import httpx
from daimon.core.config import NotebookSettings
from daimon.core.errors import DaimonError
from daimon.core.notebooks.host_client import (
    delete_blog_from_host,
    list_blogs_from_host,
)
from daimon.core.notebooks.slug import AGENT_SLUG_PATTERN, sanitize_slug

# Re-export under the historical private name; slug.py is the canonical owner.
_AGENT_SLUG_PATTERN = AGENT_SLUG_PATTERN


class HostNotConfiguredError(DaimonError):
    """Settings.notebook.host_url / admin_secret unset (D8)."""


class NotebookRateLimitError(DaimonError):
    """Principal exceeded their per-hour publish quota."""


class InvalidSlugError(DaimonError):
    """Agent-provided slug failed validation."""


_PRINCIPAL_PREFIX_BYTES = 9  # 9 bytes → 12 chars urlsafe-b64, no padding


def _principal_prefix(principal_key: str) -> str:
    digest = hashlib.blake2b(
        principal_key.encode("utf-8"),
        digest_size=_PRINCIPAL_PREFIX_BYTES,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _resolve_slug(*, agent_slug: str | None, principal_key: str | None) -> str:
    # 16 bytes = 128 bits of entropy. The slug doubles as the unauthenticated
    # access secret for /n/<slug>/* (a marimo session with kernel access on
    # the host VM), so we want it well past brute-force range.
    if agent_slug is None:
        return secrets.token_urlsafe(16)
    if principal_key is None:
        raise InvalidSlugError("principal_key is required when slug is provided")
    sanitized = sanitize_slug(agent_slug)
    return f"{_principal_prefix(principal_key)}-{sanitized}"


async def delete_blog(
    *,
    slug: str,
    notebook_settings: NotebookSettings,
    client: httpx.AsyncClient,
    principal_key: str,
) -> None:
    """Delete a blog the caller owns.

    Namespaces ``slug`` with the principal prefix so a tenant can only target
    its own blogs.
    """
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    resolved_slug = _resolve_slug(agent_slug=slug, principal_key=principal_key)
    await delete_blog_from_host(
        slug=resolved_slug,
        host_url=notebook_settings.host_url,
        admin_secret=notebook_settings.admin_secret,
        client=client,
    )


async def list_blogs(
    *,
    notebook_settings: NotebookSettings,
    client: httpx.AsyncClient,
    principal_key: str,
) -> list[dict[str, object]]:
    """List the caller's own blogs (filtered from the host's full list).

    The host exposes every tenant's blogs; we keep only those whose slug carries
    this principal's prefix so a tenant sees only what it published. Parses the
    host JSON via local ``Any``/``cast`` (same shape as host_client's
    ``_parse_cell_errors``) so strict pyright stays clean without nested-Unknown
    narrowing.
    """
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    body = await list_blogs_from_host(
        host_url=notebook_settings.host_url,
        admin_secret=notebook_settings.admin_secret,
        client=client,
    )
    prefix = _principal_prefix(principal_key)
    blogs_any: Any = body.get("blogs", [])
    if not isinstance(blogs_any, list):
        return []
    own: list[dict[str, object]] = []
    for entry in cast("list[Any]", blogs_any):
        if isinstance(entry, dict):
            blog = cast("dict[str, object]", entry)
            slug_val = blog.get("slug")
            if isinstance(slug_val, str) and slug_val.startswith(f"{prefix}-"):
                own.append(blog)
    return own
