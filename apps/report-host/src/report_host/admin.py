"""Admin routes for report-host: publish (create-or-update), delete, revoke.

Everything daimon does to a report from the publishing side goes through this
router, gated by the admin bearer — mirroring
``notebook_host.admin._bearer_dep`` in shape. The bearer alone only proves
the caller is daimon, not which tenant's report is being touched: every
report row carries its own ``tenant_id``, and the publish and delete routes
both refuse a caller-supplied tenant that does not match the existing row
(T-21-16-B, slug squatting). The publish-upload route
(``PUT /publish/{capability_token}``) is authorised by a capability token
instead and lives in a separate module — the two authorisation mechanisms
never mix in one place.
"""

import asyncio
import hmac
import re
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from report_host import reports_store
from report_host.config import Settings

# Lowercase alphanumerics and hyphens only, no leading/trailing hyphen, bounded
# length. The slug becomes both a directory name and a URL segment, so a
# separator or a ".." is a filesystem bug waiting to happen (T-21-16-D).
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLUG_MAX_LEN = 64

# Deliberately silent about which tenant already owns the slug (T-21-16-C).
_TENANT_MISMATCH_MESSAGE = "this slug belongs to a different tenant"


def _validate_slug(slug: str) -> str:
    """Reject anything that is not a safe directory name and URL segment.

    Called before any store lookup or path construction in every handler
    below.
    """
    if not slug or len(slug) > _SLUG_MAX_LEN or not _SLUG_PATTERN.fullmatch(slug):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"invalid slug: {slug!r}")
    return slug


def _bearer_dep(settings: Settings) -> Callable[[str | None], None]:
    def require(authorization: str | None = Header(default=None)) -> None:
        provided = authorization or ""
        # No short-circuit: comparing every entry avoids a timing leak of
        # which secret, if any, matched (T-21-16-A).
        matched = False
        for secret in settings.admin_secrets:
            expected = f"Bearer {secret.get_secret_value()}"
            if hmac.compare_digest(provided, expected):
                matched = True
        if not matched:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    return require


class RecipientIn(BaseModel):
    name: str
    label: str


class PublishRequest(BaseModel):
    title: str
    tenant_id: str
    agent_name: str
    cap_usd: Decimal
    seam_token: str
    recipients: list[RecipientIn]


class RecipientLink(BaseModel):
    name: str
    link: str


class PublishResponse(BaseModel):
    # No seam_token here — it must never be echoed back into a response or a
    # log (T-21-16-F).
    slug: str
    links: list[RecipientLink]


class DeleteResponse(BaseModel):
    deleted: bool


def build_admin_router(
    *,
    settings: Settings,
    conn_factory: Callable[[], sqlite3.Connection],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Build the admin router: publish (create-or-update), delete, revoke.

    ``conn_factory`` is called exactly once, here, mirroring the reader
    router's single-shared-connection design (production passes a factory
    calling ``reports_store.connect(settings.data_dir)``; tests pass a
    lambda over an already-open ``tmp_path`` connection). ``now`` is
    injected so tests control every timestamp without a real clock.

    The bearer dependency is attached at the router level rather than
    per-route, so a route added here later can never forget it.
    """
    conn = conn_factory()
    require_admin = _bearer_dep(settings)
    slug_locks: dict[str, asyncio.Lock] = {}

    def lock_for(slug: str) -> asyncio.Lock:
        lock = slug_locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            slug_locks[slug] = lock
        return lock

    router = APIRouter(dependencies=[Depends(require_admin)])

    @router.put("/admin/reports/{slug}")
    async def put_report(slug: str, body: PublishRequest) -> PublishResponse:  # pyright: ignore[reportUnusedFunction]
        """Create a report, or replace its metadata and recipient set.

        Refuses 403 when an existing row belongs to a different tenant —
        one tenant must not take over another tenant's slug by publishing
        over it. Otherwise ``save_report`` preserves spend, the served PDF
        and the bundle reference (T-21-16-E); every previously issued
        recipient link is revoked and a fresh one minted per submitted
        recipient, since a re-publish replaces the recipient set outright
        rather than diffing old against new.
        """
        validated_slug = _validate_slug(slug)
        async with lock_for(validated_slug):
            existing = reports_store.load_report(conn, slug=validated_slug)
            if existing is not None and existing.tenant_id != body.tenant_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=_TENANT_MISMATCH_MESSAGE)

            reports_store.save_report(
                conn,
                slug=validated_slug,
                title=body.title,
                tenant_id=body.tenant_id,
                agent_name=body.agent_name,
                cap_usd=body.cap_usd,
                agent_token=body.seam_token,
                now=now(),
            )

            for recipient in reports_store.list_recipients(conn, slug=validated_slug):
                if recipient.revoked_at is None:
                    reports_store.revoke_recipient(
                        conn, slug=validated_slug, token=recipient.token, now=now()
                    )

            links: list[RecipientLink] = []
            for recipient_in in body.recipients:
                token = secrets.token_urlsafe(24)
                reports_store.add_recipient(
                    conn,
                    slug=validated_slug,
                    name=recipient_in.name,
                    label=recipient_in.label,
                    token=token,
                    now=now(),
                    ttl_days=settings.recipient_link_ttl_days,
                )
                links.append(
                    RecipientLink(
                        name=recipient_in.name,
                        link=f"{settings.public_url_base}r/{validated_slug}?k={token}",
                    )
                )
        return PublishResponse(slug=validated_slug, links=links)

    @router.delete("/admin/reports/{slug}")
    async def delete_report_route(slug: str, tenant_id: str) -> DeleteResponse:  # pyright: ignore[reportUnusedFunction]
        """Delete a report (cascading recipients, revisions and threads).

        Reports ``deleted`` rather than an unconditional success — a caller
        that cannot tell "I removed it" from "there was nothing by that
        name" reports a typo'd slug as a real deletion (T-21-16-G). Refuses
        403 on a tenant mismatch, leaving the row and its directory
        untouched.
        """
        validated_slug = _validate_slug(slug)
        async with lock_for(validated_slug):
            existing = reports_store.load_report(conn, slug=validated_slug)
            if existing is not None and existing.tenant_id != tenant_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=_TENANT_MISMATCH_MESSAGE)
            deleted = reports_store.delete_report(conn, slug=validated_slug)
            report_dir = settings.data_dir / validated_slug
            # A validated slug can never escape data_dir, but this is the
            # one place that removes a directory tree, so it re-asserts the
            # containment it depends on rather than trusting validation
            # alone (T-21-16-D).
            if report_dir.parent == settings.data_dir:
                shutil.rmtree(report_dir, ignore_errors=True)
        return DeleteResponse(deleted=deleted)

    @router.delete("/admin/reports/{slug}/recipients/{token}")
    async def delete_recipient_route(slug: str, token: str) -> DeleteResponse:  # pyright: ignore[reportUnusedFunction]
        """Revoke one recipient's link. Revoking an already-revoked link is not an error."""
        validated_slug = _validate_slug(slug)
        revoked = reports_store.revoke_recipient(conn, slug=validated_slug, token=token, now=now())
        return DeleteResponse(deleted=revoked)

    return router
