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
# How long a read-only scratch notebook is kept when the agent asks for
# nothing, and the longest it may ask for. The host clamps to its own maximum.
_DEFAULT_TTL_DAYS = 1
_MAX_TTL_DAYS = 365
_SECONDS_PER_DAY = 86400
# 72-bit url-safe nonce for single-use jti dedup (host burns it after one
# upload). This is dedup entropy, NOT access-secret strength; notebook access
# is the host's per-notebook token in the returned link.
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
    tenant: str | None = None,
    notebook_ttl_seconds: int | None = None,
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
        tenant=tenant,
        notebook_ttl_seconds=notebook_ttl_seconds,
    )
    return {
        "upload_url": f"{host.rstrip('/')}/upload/{token}",
        "slug": slug,
        "upload_expires_at": (now + timedelta(seconds=_UPLOAD_TTL_SECONDS)).isoformat(),
    }


def create_notebook_upload(
    *,
    slug: str | None = None,
    permanent: bool = False,
    editable: bool = False,
    ttl_days: int | None = None,
    notebook_settings: NotebookSettings,
    principal_key: str | None = None,
    tenant: str | None = None,
    now: datetime,
    rate_limiter: RateLimiter | None = None,
) -> dict[str, str]:
    """Mint an upload URL for a notebook.

    ``permanent`` picks the host's two shapes: False mints an ephemeral
    scratch notebook (TTL-reaped), True mints a run-mode blog that survives
    restarts and is never reaped. It is one flag
    rather than two minters because everything else — the slug namespace, the
    single-use token, the rate-limit charge, the curl the agent then runs — is
    identical, and a caller choosing between two tool names at mint time has to
    decide permanence before it knows whether the notebook is any good.

    A scratch notebook is a read-only app unless ``editable`` asks for the
    marimo editor. The editor runs arbitrary code on the shared notebook host
    for anyone holding the link, so it is opt-in and never for a blog.

    ``ttl_days`` (1 to 365, default 1) is how long the host keeps a read-only
    scratch notebook. A blog is kept until deleted, and the editor for the
    host's own TTL, so neither takes one.

    ``slug`` None → a fresh random slug; otherwise principal-namespaced.
    """
    if permanent and editable:
        raise ValueError("a permanent blog cannot be editable; publish it read-only")
    if ttl_days is not None and (permanent or editable):
        raise ValueError(
            "ttl_days is only for a read-only scratch notebook; a blog is kept until "
            "deleted and the editor lasts the host's own TTL"
        )
    if ttl_days is not None and not _DEFAULT_TTL_DAYS <= ttl_days <= _MAX_TTL_DAYS:
        raise ValueError(f"ttl_days must be between 1 and {_MAX_TTL_DAYS}, got {ttl_days}")
    if editable and not notebook_settings.allow_editable:
        raise ValueError(
            "editable notebooks are off on this deployment (notebook.allow_editable); "
            "publish it read-only"
        )
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
        op="blog" if permanent else "notebook_edit" if editable else "notebook",
        max_bytes=notebook_settings.max_source_bytes,
        now=now,
        name=None,
        tenant=tenant,
        notebook_ttl_seconds=(
            None if permanent or editable else (ttl_days or _DEFAULT_TTL_DAYS) * _SECONDS_PER_DAY
        ),
    )


def create_attachment_upload(
    *,
    slug: str,
    name: str,
    notebook_settings: NotebookSettings,
    principal_key: str,
    now: datetime,
    rate_limiter: RateLimiter | None = None,
    tenant: str | None = None,
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
        tenant=tenant,
    )
