"""Publishing orchestration: resolve, derive, mint, register, hand back an upload URL.

Direct analog of ``daimon.core.notebooks.publish``: a not-configured error, a
slug error, and free functions taking explicit settings plus a tenant/account
pair — no adapter imports, no FastMCP, no tool-error type, so this can be
unit-tested without an MCP ``Context``. The wrapper that maps these errors
into ``ToolError`` lives in the MCP adapter, a separate plan.

Order matters in ``publish_report`` and is the substance of that function:
validate the slug and the host configuration before anything is created,
derive the reader variant before any token is minted, mint the token before
registering with the host, and revoke that token if registration fails. Get
the order wrong and a report exists on the host with no token, or a token
exists with no report — both are states nobody will ever clean up.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime
from decimal import Decimal

import httpx
from anthropic import AsyncAnthropic
from daimon.core import ma_identity
from daimon.core.config import ReportHostSettings
from daimon.core.errors import DaimonError
from daimon.core.mcp_auth import mint_agent_mcp_token
from daimon.core.notebooks.capability import mint_token
from daimon.core.reader_agent import ensure_reader_variant
from daimon.core.reports.host_client import (
    Recipient,
    ReportHostError,
    delete_admin_report,
    put_admin_report,
)
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.mcp_tokens import list_live_tokens_by_label, revoke_mcp_token
from daimon.core.stores.scoped_config_read import resolve
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "HostNotConfiguredError",
    "InvalidSlugError",
    "PublishResult",
    "DeleteResult",
    "publish_report",
    "delete_report",
]


class HostNotConfiguredError(DaimonError):
    """``ReportHostSettings.host_url`` / ``admin_secret`` unset."""


class InvalidSlugError(DaimonError):
    """Caller-provided slug failed the host's own validation pattern."""


# Mirrors the report host's own slug pattern exactly
# (report_host.admin._SLUG_PATTERN) — a slug the host will reject must not
# first cause an agent to be derived and a token to be minted.
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLUG_MAX_LEN = 64

# The one place the mcp_tokens.label format is defined. publish_report mints
# under this label; delete_report looks tokens back up by it. Never format
# this string a second time anywhere else in this module.
_REPORT_TOKEN_LABEL_PREFIX = "report:"

# 5 minutes: long enough for the publishing agent to curl its bundle to the
# host, short enough to bound the replay window on the single-use
# capability. Mirrors daimon.core.notebooks.upload's _UPLOAD_TTL_SECONDS.
_UPLOAD_TTL_SECONDS = 300
# 72-bit url-safe nonce for single-use jti dedup, matching
# daimon.core.notebooks.upload's _JTI_BYTES.
_JTI_BYTES = 9


def _report_token_label(slug: str) -> str:
    """The mcp_tokens.label format shared by minting (publish) and lookup (delete)."""
    return f"{_REPORT_TOKEN_LABEL_PREFIX}{slug}"


def _validate_slug(slug: str) -> None:
    if not slug or len(slug) > _SLUG_MAX_LEN or not _SLUG_PATTERN.fullmatch(slug):
        raise InvalidSlugError(f"invalid report slug: {slug!r}")


class PublishResult(BaseModel):
    """What a caller of ``publish_report`` gets back."""

    model_config = ConfigDict(frozen=True)

    upload_url: str
    links: dict[str, str]


class DeleteResult(BaseModel):
    """What a caller of ``delete_report`` gets back.

    Carries both counts so the caller can tell "deleted" from "there was
    nothing there" — a report deleted twice, or a report that never
    existed, must not look like a successful first deletion.
    """

    model_config = ConfigDict(frozen=True)

    tokens_revoked: int
    host_removed: bool


async def publish_report(
    *,
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    report_host_settings: ReportHostSettings,
    jwt_secret: bytes,
    max_bundle_bytes: int,
    default: DeploymentDefault,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    slug: str,
    title: str,
    recipients: list[Recipient],
    cap_usd: Decimal,
    agent: str | None,
    now: datetime,
) -> PublishResult:
    """Resolve the source agent, derive its reader variant, mint the report its
    own token, register it with the host, and return a one-time upload URL.

    ``jwt_secret`` and ``max_bundle_bytes`` are taken as plain values rather
    than a settings block: the only two things this function needs off
    ``McpSettings``, injected explicitly per guideline:architecture rather
    than handing the whole settings block to a function that uses two of
    its six fields. The caller (the MCP tool wrapper) is responsible for
    checking ``settings.mcp.jwt_secret`` is configured before calling —
    the same responsibility the CLI's ``mint-agent-token`` command already
    carries for the same primitive.

    ``agent`` is the publisher's explicit choice of source agent. When
    omitted, the tenant's configured agent is resolved through the shared
    channel/tenant/deployment cascade (``default`` is that cascade's bottom
    tier, ``daimon.core.defaults.loader.parse_deployment_default``'s
    output) — nothing about the in-session MCP credential identifies which
    agent is calling, so there is no inference to attempt here; do not add
    one later.

    The freshly minted report token is minted under ``account_id`` /
    ``tenant_id`` — the publisher's own account, which carries a platform
    principal. This is the billing decision the whole phase turns on: every
    reader question against this report runs through the normal balance and
    cap admission checks and lands in the publishing tenant's usage, exactly
    as if the publisher had asked the question themselves. Minting under
    any other account would make reader Q&A unbilled and un-admission-gated.

    Order (see module docstring): slug validation and the not-configured
    check both happen before the reader variant is derived; the variant is
    derived before any token is minted; the token is minted before the host
    registration; a failed host registration revokes the token it just
    minted before propagating, so a report that never made it onto the host
    never leaves a live, billable credential behind.
    """
    _validate_slug(slug)
    if report_host_settings.host_url is None or report_host_settings.admin_secret is None:
        raise HostNotConfiguredError(
            "report host not configured: host_url and admin_secret are required"
        )

    resolved_agent = agent
    if resolved_agent is None:
        async with session_factory() as session:
            resolved_config = await resolve(
                session,
                context=ScopeContext(tenant_id=tenant_id, account_id=account_id),
                default=default,
            )
        if resolved_config.agent_name is None:
            raise DaimonError(
                f"no agent configured for tenant {tenant_id} and no --agent was given"
            )
        resolved_agent = resolved_config.agent_name

    variant = await ensure_reader_variant(
        anthropic, tenant_id=tenant_id, account_id=account_id, source_name=resolved_agent
    )

    label = _report_token_label(slug)
    agent_uuid = ma_identity.derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=variant.id)
    async with session_factory() as session, session.begin():
        seam_token = await mint_agent_mcp_token(
            session,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            label=label,
            secret=jwt_secret,
            now=now,
        )

    try:
        registered = await put_admin_report(
            client=http_client,
            settings=report_host_settings,
            slug=slug,
            title=title,
            tenant_id=tenant_id,
            agent_name=resolved_agent,
            cap_usd=cap_usd,
            seam_token=seam_token,
            recipients=recipients,
        )
    except ReportHostError:
        # An orphaned live credential for a report that never registered is
        # a secret nobody will ever clean up — revoke it before propagating.
        async with session_factory() as session, session.begin():
            live = await list_live_tokens_by_label(session, tenant_id=tenant_id, label=label)
            for row in live:
                await revoke_mcp_token(session, jti=row.jti, now=now)
        raise

    capability = mint_token(
        report_host_settings.admin_secret.get_secret_value(),
        slug=slug,
        op="report",
        max_bytes=max_bundle_bytes,
        now=now,
        jti=secrets.token_urlsafe(_JTI_BYTES),
        ttl_seconds=_UPLOAD_TTL_SECONDS,
    )
    upload_url = f"{str(report_host_settings.host_url).rstrip('/')}/publish/{capability}"

    return PublishResult(upload_url=upload_url, links=registered.links)


async def delete_report(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    report_host_settings: ReportHostSettings,
    tenant_id: uuid.UUID,
    slug: str,
    now: datetime,
) -> DeleteResult:
    """Revoke a report's token(s), then remove it from the host, in that order.

    Revoking first means the failure window (a host call that fails after a
    successful revoke) leaves a report that is unreachable with a dead
    credential — a safe end state. The reverse order would leave a live,
    billable credential for a report nobody can see, which is not.

    Deleting a report with no live tokens and no host row is not an error:
    a report deleted twice, or one that never existed, answers with a
    ``DeleteResult`` reporting zero/False rather than raising.
    """
    if report_host_settings.host_url is None or report_host_settings.admin_secret is None:
        raise HostNotConfiguredError(
            "report host not configured: host_url and admin_secret are required"
        )

    label = _report_token_label(slug)
    async with session_factory() as session, session.begin():
        live = await list_live_tokens_by_label(session, tenant_id=tenant_id, label=label)
        for row in live:
            await revoke_mcp_token(session, jti=row.jti, now=now)

    host_removed = await delete_admin_report(
        client=http_client, settings=report_host_settings, slug=slug, tenant_id=tenant_id
    )
    return DeleteResult(tokens_revoked=len(live), host_removed=host_removed)
