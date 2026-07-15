"""Pure minters: build a capability-upload URL for the notebook host.

These do NO host I/O. They resolve the principal-namespaced slug, charge the
rate limit, and mint a signed capability token, returning the opaque
``upload_url`` the agent curls its sandbox file to. The bytes never pass through
the model token stream — the whole point of this module. ``now`` is injected so
the functions stay deterministic in tests; the single-use ``jti`` is generated
here with ``secrets`` (matching how ``_resolve_slug`` mints random slugs).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from daimon.core.config import NotebookSettings
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.notebooks.attach import (
    _ATTACHMENT_NAME_PATTERN,  # pyright: ignore[reportPrivateUsage]  # attach.py owns the pattern
    InvalidAttachmentError,
)
from daimon.core.notebooks.capability import Op, mint_token
from daimon.core.notebooks.publish import (
    HostNotConfiguredError,
    NotebookRateLimitError,
    _resolve_slug,  # pyright: ignore[reportPrivateUsage]  # publish.py owns slug resolution
)

# 5 min: long enough for an agent to curl a sandbox file to the host, short
# enough to bound the replay window on the single-use token.
_UPLOAD_TTL_SECONDS = 300
# 72-bit url-safe nonce for single-use jti dedup (host burns it after one
# upload). This is dedup entropy, NOT access-secret strength — the slug is the
# access secret; see _resolve_slug's token_urlsafe(16).
_JTI_BYTES = 9


def _mint_url(
    *,
    host: str,
    secret: str,
    slug: str,
    op: Op,
    max_bytes: int,
    now: datetime,
    name: str | None,
) -> dict[str, str]:
    """Mint a token for ``slug``/``op`` and wrap it in the host upload URL."""
    token = mint_token(
        secret,
        slug=slug,
        op=op,
        max_bytes=max_bytes,
        now=now,
        jti=secrets.token_urlsafe(_JTI_BYTES),
        ttl_seconds=_UPLOAD_TTL_SECONDS,
        name=name,
    )
    return {
        "upload_url": f"{host.rstrip('/')}/upload/{token}",
        "slug": slug,
        "upload_expires_at": (now + timedelta(seconds=_UPLOAD_TTL_SECONDS)).isoformat(),
    }


def create_blog_upload(
    *,
    slug: str,
    notebook_settings: NotebookSettings,
    principal_key: str,
    now: datetime,
    rate_limiter: RateLimiter | None = None,
) -> dict[str, str]:
    """Mint an upload URL for a permanent run-mode blog under ``slug``."""
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    resolved_slug = _resolve_slug(agent_slug=slug, principal_key=principal_key)
    # Charged at mint time, no refund: minting performs no host call that
    # could fail (unlike publish.py). One token mints → at most one host
    # spawn (single-use jti), so capping mints caps spawns.
    if rate_limiter is not None and not rate_limiter.check_and_record(principal_key):
        raise NotebookRateLimitError(
            f"publish rate limit exceeded for principal {principal_key!r}: "
            f"max {rate_limiter.max_requests}/hour"
        )
    return _mint_url(
        host=str(notebook_settings.host_url),
        secret=notebook_settings.admin_secret.get_secret_value(),
        slug=resolved_slug,
        op="blog",
        max_bytes=notebook_settings.max_source_bytes,
        now=now,
        name=None,
    )


def create_notebook_upload(
    *,
    slug: str | None = None,
    notebook_settings: NotebookSettings,
    principal_key: str | None = None,
    now: datetime,
    rate_limiter: RateLimiter | None = None,
) -> dict[str, str]:
    """Mint an upload URL for an ephemeral edit-mode notebook.

    ``slug`` None → a fresh random slug; otherwise principal-namespaced.
    """
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    resolved_slug = _resolve_slug(agent_slug=slug, principal_key=principal_key)
    # Charged at mint time, no refund: minting performs no host call that
    # could fail (unlike publish.py). One token mints → at most one host
    # spawn (single-use jti), so capping mints caps spawns.
    if (
        rate_limiter is not None
        and principal_key is not None
        and not rate_limiter.check_and_record(principal_key)
    ):
        raise NotebookRateLimitError(
            f"publish rate limit exceeded for principal {principal_key!r}: "
            f"max {rate_limiter.max_requests}/hour"
        )
    return _mint_url(
        host=str(notebook_settings.host_url),
        secret=notebook_settings.admin_secret.get_secret_value(),
        slug=resolved_slug,
        op="notebook",
        max_bytes=notebook_settings.max_source_bytes,
        now=now,
        name=None,
    )


def create_attachment_upload(
    *,
    slug: str,
    name: str,
    notebook_settings: NotebookSettings,
    principal_key: str,
    now: datetime,
    rate_limiter: RateLimiter | None = None,
) -> dict[str, str]:
    """Mint an upload URL for a raw data file at ``data/<name>`` under ``slug``."""
    if notebook_settings.host_url is None or notebook_settings.admin_secret is None:
        raise HostNotConfiguredError("notebook host not configured")
    if not _ATTACHMENT_NAME_PATTERN.fullmatch(name):
        raise InvalidAttachmentError(
            f"attachment name must match [A-Za-z0-9_][A-Za-z0-9_.-]{{0,63}}, got: {name!r}"
        )
    resolved_slug = _resolve_slug(agent_slug=slug, principal_key=principal_key)
    # Charged at mint time, no refund: minting performs no host call that
    # could fail (unlike publish.py). One token mints → at most one host
    # spawn (single-use jti), so capping mints caps spawns.
    if rate_limiter is not None and not rate_limiter.check_and_record(principal_key):
        raise NotebookRateLimitError(
            f"notebook rate limit exceeded for principal {principal_key!r}: "
            f"max {rate_limiter.max_requests}/hour"
        )
    return _mint_url(
        host=str(notebook_settings.host_url),
        secret=notebook_settings.admin_secret.get_secret_value(),
        slug=resolved_slug,
        op="data",
        max_bytes=notebook_settings.max_attachment_bytes,
        now=now,
        name=name,
    )
