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
    delete_notebook_from_host,
    list_blogs_from_host,
    list_notebooks_from_host,
)
from daimon.core.notebooks.slug import sanitize_slug


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
    # 16 bytes = 128 bits of entropy. Access is gated by the per-notebook
    # token the host puts in the link, not the slug (the slug is visible to
    # other notebooks' code on the host), but a random slug still keeps
    # links from colliding or being guessed into a 409.
    if agent_slug is None:
        return secrets.token_urlsafe(16)
    if principal_key is None:
        raise InvalidSlugError("principal_key is required when slug is provided")
    sanitized = sanitize_slug(agent_slug)
    return f"{_principal_prefix(principal_key)}-{sanitized}"


async def delete_notebook(
    *,
    slug: str,
    notebook_settings: NotebookSettings,
    client: httpx.AsyncClient,
    principal_key: str,
) -> bool:
    """Delete a notebook the caller owns; True if one was actually removed.

    ``slug`` is the bare authoring name — the same form ``list_notebooks``
    returns and ``create_notebook_upload`` accepts. It is namespaced with the
    principal prefix here, exactly once, so a tenant can only target its own.
    Passing an already-namespaced slug would prefix it twice and silently
    target nothing, which is why nothing in this module ever hands a caller
    the namespaced form.
    """
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    resolved_slug = _resolve_slug(agent_slug=slug, principal_key=principal_key)
    return await delete_notebook_from_host(
        slug=resolved_slug,
        host_url=notebook_settings.host_url,
        admin_secret=notebook_settings.admin_secret,
        client=client,
    )


def _strip_principal_prefix(slug: str, prefix: str) -> str:
    return slug[len(prefix) + 1 :]


async def list_notebooks(
    *,
    notebook_settings: NotebookSettings,
    client: httpx.AsyncClient,
    principal_key: str,
) -> list[dict[str, object]]:
    """List the caller's own notebooks, permanent and ephemeral alike.

    The host exposes every tenant's; we keep only those whose slug carries this
    principal's prefix, then hand back the **bare** slug so the value round-trips
    into ``delete_notebook`` and ``create_notebook_upload``. ``url`` keeps the
    namespaced slug, because that is the real address.

    Each entry carries ``permanent``: True for a run-mode blog (survives
    restarts, never reaped), False for an edit-mode scratch notebook (TTL). Both
    kinds appear because both are things the caller published and may want back.

    Parses the host JSON via local ``Any``/``cast`` (same shape as host_client's
    ``_parse_cell_errors``) so strict pyright stays clean without nested-Unknown
    narrowing.
    """
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    blogs_body = await list_blogs_from_host(
        host_url=notebook_settings.host_url,
        admin_secret=notebook_settings.admin_secret,
        client=client,
    )
    notebooks_body = await list_notebooks_from_host(
        host_url=notebook_settings.host_url,
        admin_secret=notebook_settings.admin_secret,
        client=client,
    )
    prefix = _principal_prefix(principal_key)
    blog_entries = _own_entries(blogs_body.get("blogs", []), prefix)
    process_entries = _own_entries(notebooks_body.get("notebooks", []), prefix)
    blog_slugs = {cast("str", entry["slug"]) for entry in blog_entries}
    own: list[dict[str, object]] = []
    seen: set[str] = set()
    # Blogs first so a live blog's richer record (created_at, title) wins over
    # its process-list twin; the process list then adds anything not registered.
    for entry in (*blog_entries, *process_entries):
        namespaced = cast("str", entry["slug"])
        if namespaced in seen:
            continue
        seen.add(namespaced)
        own.append(
            {
                **entry,
                "slug": _strip_principal_prefix(namespaced, prefix),
                "permanent": namespaced in blog_slugs,
            }
        )
    return own


def _own_entries(entries_any: object, prefix: str) -> list[dict[str, object]]:
    """The dict entries whose ``slug`` carries ``prefix``, in host order."""
    if not isinstance(entries_any, list):
        return []
    own: list[dict[str, object]] = []
    for entry in cast("list[Any]", entries_any):
        if isinstance(entry, dict):
            item = cast("dict[str, object]", entry)
            slug_val = item.get("slug")
            if isinstance(slug_val, str) and slug_val.startswith(f"{prefix}-"):
                own.append(item)
    return own
