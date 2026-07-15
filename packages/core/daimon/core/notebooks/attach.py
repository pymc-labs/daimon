"""attach_notebook_data orchestration: validate, namespace, rate-limit, PUT."""

from __future__ import annotations

import re

import httpx
from daimon.core.config import NotebookSettings
from daimon.core.errors import DaimonError
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.notebooks.host_client import NotebookHostError, attach_to_host
from daimon.core.notebooks.publish import (
    HostNotConfiguredError,
    NotebookRateLimitError,
    _resolve_slug,  # pyright: ignore[reportPrivateUsage]  # publish.py owns slug resolution
)


class InvalidAttachmentError(DaimonError):
    """Attachment name or size failed validation."""


_ATTACHMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


async def attach_notebook_data(
    *,
    content: bytes,
    name: str,
    notebook_settings: NotebookSettings,
    client: httpx.AsyncClient,
    principal_key: str | None = None,
    rate_limiter: RateLimiter | None = None,
    slug: str | None = None,
) -> dict[str, object]:
    """Validate, namespace, rate-limit, PUT to the host.

    Returns ``{slug, name, size_bytes, path}`` from the host, with ``slug``
    replaced by the principal-namespaced resolved slug. ``path`` is
    ``data/<name>`` — the canonical agent-visible read path.
    """
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    if not _ATTACHMENT_NAME_PATTERN.fullmatch(name):
        raise InvalidAttachmentError(
            f"attachment name must match [A-Za-z0-9_][A-Za-z0-9_.-]{{0,63}}, got: {name!r}"
        )
    if len(content) > notebook_settings.max_attachment_bytes:
        raise InvalidAttachmentError(
            f"attachment exceeds max_attachment_bytes "
            f"({len(content)} > {notebook_settings.max_attachment_bytes})"
        )
    resolved_slug = _resolve_slug(agent_slug=slug, principal_key=principal_key)
    if (
        rate_limiter is not None
        and principal_key is not None
        and not rate_limiter.check_and_record(principal_key)
    ):
        raise NotebookRateLimitError(
            f"notebook rate limit exceeded for principal {principal_key!r}: "
            f"max {rate_limiter.max_requests}/hour"
        )
    try:
        body = await attach_to_host(
            slug=resolved_slug,
            name=name,
            content=content,
            host_url=notebook_settings.host_url,
            admin_secret=notebook_settings.admin_secret,
            client=client,
        )
    except (NotebookHostError, httpx.HTTPError):
        # Don't burn the principal's quota for host flake.
        if rate_limiter is not None and principal_key is not None:
            rate_limiter.refund(principal_key)
        raise
    result: dict[str, object] = dict(body)
    result["slug"] = resolved_slug
    return result
