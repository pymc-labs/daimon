"""Thin httpx client to the report host's admin API.

Direct analog of ``daimon.core.notebooks.host_client``: a domain-error
subclass for any non-2xx or transport failure, free async functions taking
an injected ``httpx.AsyncClient`` (no client construction here), and a
uniform non-2xx message carrying the status and a truncated body. The one
deliberate difference from the notebook sibling is that the bearer + host
URL are threaded through as a single ``ReportHostSettings`` block rather
than two loose parameters — this module's three functions all need exactly
that pair, and checking both together is what lets ``_require_configured``
refuse to speak before any request is issued.

Every request body and response is a typed Pydantic model, never a bare
untyped mapping — a response missing an expected field raises a
``ValidationError`` (wrapped as ``ReportHostError``) instead of silently
returning a half-populated result.

Never logs the seam token or the admin bearer — the uniform error message
carries only the response status and a truncated body, never the request
headers or the request body.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
from daimon.core.config import ReportHostSettings
from daimon.core.errors import DaimonError
from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr, ValidationError

__all__ = [
    "ReportHostError",
    "Recipient",
    "ReportRegistered",
    "put_admin_report",
    "delete_admin_report",
    "revoke_admin_recipient",
]

# Conservative per-call timeout, matching the notebook host client's sibling
# calls (host_client.attach_to_host and friends all use 30.0).
_TIMEOUT_SECONDS = 30.0


class ReportHostError(DaimonError):
    """Raised when the report host rejects a call, fails a call, or is unconfigured."""


class Recipient(BaseModel):
    """One named recipient of a published report; the host mints one link each."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str


class ReportRegistered(BaseModel):
    """The host's answer to a publish (create-or-replace) call.

    ``links`` maps each recipient's ``name`` to the link the host minted for
    them — a dict, not the host's own list-of-objects wire shape, because
    every caller of this function wants "the link for Ada", not a list to
    scan.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    links: dict[str, str]


class _RecipientBody(BaseModel):
    """Request-body shape for one recipient, matching the host's ``RecipientIn``."""

    name: str
    label: str


class _PublishBody(BaseModel):
    """Request-body shape for ``PUT /admin/reports/{slug}``.

    Field names and types must match the host's ``PublishRequest`` exactly —
    that model is what decides them. ``cap_usd`` is serialised as a string
    (never a bare float) so the host's ``Decimal`` field parses it precisely.
    """

    title: str
    tenant_id: str
    agent_name: str
    cap_usd: str
    seam_token: str
    recipients: list[_RecipientBody]


class _RecipientLinkWire(BaseModel):
    name: str
    link: str


class _ReportRegisteredWire(BaseModel):
    """Response-body shape from ``PUT /admin/reports/{slug}``."""

    slug: str
    links: list[_RecipientLinkWire]


class _DeletedWire(BaseModel):
    """Response-body shape shared by both delete/revoke routes."""

    deleted: bool


def _require_configured(settings: ReportHostSettings) -> tuple[HttpUrl, SecretStr]:
    """Return (host_url, admin_secret), or raise before any request is sent.

    Refusing here — rather than letting an unauthenticated request go out
    and fail on the wire — is the point: a deployment with no report host
    configured must never emit a bearer-less call.
    """
    if settings.host_url is None:
        raise ReportHostError("report host not configured: host_url is unset")
    if settings.admin_secret is None:
        raise ReportHostError("report host not configured: admin_secret is unset")
    return settings.host_url, settings.admin_secret


def _bearer_headers(admin_secret: SecretStr) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_secret.get_secret_value()}"}


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return await client.request(method, url, timeout=_TIMEOUT_SECONDS, **kwargs)
    except httpx.HTTPError as exc:
        raise ReportHostError(f"report host request failed: {exc}") from exc


async def put_admin_report(
    *,
    client: httpx.AsyncClient,
    settings: ReportHostSettings,
    slug: str,
    title: str,
    tenant_id: uuid.UUID,
    agent_name: str,
    cap_usd: Decimal,
    seam_token: str,
    recipients: list[Recipient],
) -> ReportRegistered:
    """``PUT /admin/reports/{slug}`` — create or replace a report and its recipients."""
    host_url, admin_secret = _require_configured(settings)
    url = f"{str(host_url).rstrip('/')}/admin/reports/{slug}"
    body = _PublishBody(
        title=title,
        tenant_id=str(tenant_id),
        agent_name=agent_name,
        cap_usd=str(cap_usd),
        seam_token=seam_token,
        recipients=[_RecipientBody(name=r.name, label=r.label) for r in recipients],
    )
    r = await _send(
        client,
        "PUT",
        url,
        json=body.model_dump(mode="json"),
        headers=_bearer_headers(admin_secret),
    )
    if not r.is_success:
        raise ReportHostError(f"report host returned {r.status_code}: {r.text[:200]}")
    try:
        wire = _ReportRegisteredWire.model_validate(r.json())
    except ValidationError as exc:
        raise ReportHostError(f"report host response missing expected field: {exc}") from exc
    return ReportRegistered(slug=wire.slug, links={link.name: link.link for link in wire.links})


async def delete_admin_report(
    *,
    client: httpx.AsyncClient,
    settings: ReportHostSettings,
    slug: str,
    tenant_id: uuid.UUID,
) -> bool:
    """``DELETE /admin/reports/{slug}?tenant_id=``; True if something was actually removed."""
    host_url, admin_secret = _require_configured(settings)
    url = f"{str(host_url).rstrip('/')}/admin/reports/{slug}"
    r = await _send(
        client,
        "DELETE",
        url,
        params={"tenant_id": str(tenant_id)},
        headers=_bearer_headers(admin_secret),
    )
    if not r.is_success:
        raise ReportHostError(f"report host returned {r.status_code}: {r.text[:200]}")
    try:
        return _DeletedWire.model_validate(r.json()).deleted
    except ValidationError as exc:
        raise ReportHostError(f"report host response missing expected field: {exc}") from exc


async def revoke_admin_recipient(
    *,
    client: httpx.AsyncClient,
    settings: ReportHostSettings,
    slug: str,
    token: str,
) -> bool:
    """``DELETE /admin/reports/{slug}/recipients/{token}``; True if a link was revoked."""
    host_url, admin_secret = _require_configured(settings)
    url = f"{str(host_url).rstrip('/')}/admin/reports/{slug}/recipients/{token}"
    r = await _send(client, "DELETE", url, headers=_bearer_headers(admin_secret))
    if not r.is_success:
        raise ReportHostError(f"report host returned {r.status_code}: {r.text[:200]}")
    try:
        return _DeletedWire.model_validate(r.json()).deleted
    except ValidationError as exc:
        raise ReportHostError(f"report host response missing expected field: {exc}") from exc
