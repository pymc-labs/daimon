"""``daimon-web owner-link``: a one-time link that makes whoever opens it the owner.

For a new deployment, where nobody can grant roles in the web app yet. The person signs
in first, then opens the link. ``--revoke`` cancels the links nobody has opened.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime, timedelta

from daimon.adapters.web.audit import audit
from daimon.adapters.web.invites import issue_link, link_url
from daimon.core.config import Settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_invites
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="daimon-web owner-link", description=__doc__)
    parser.add_argument("--workspace", type=uuid.UUID, required=True, help="Workspace (tenant) id.")
    parser.add_argument("--days", type=int, default=1, help="Days the link stays usable.")
    parser.add_argument(
        "--revoke", action="store_true", help="Revoke the workspace's unopened owner links."
    )
    return parser.parse_args(argv)


def owner_link_engine(settings: Settings) -> AsyncEngine:
    """A small pool for the one-off command, still the four connections session fences need."""
    return build_engine(str(settings.database.url), pool_size=1, max_overflow=3)


async def make_owner_link(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    days: int,
    now: datetime,
) -> str:
    assert settings.web is not None
    async with sessionmaker.begin() as db:
        if await tenants_store.get_tenant(db, tenant_id) is None:
            raise LookupError(f"no workspace {tenant_id}")
        _, token = await issue_link(
            db, tenant_id=tenant_id, kind="owner", expires_at=now + timedelta(days=days)
        )
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=None,
            action="Created owner link",
            target="Owner link",
            detail=f"{days} days",
            at=now,
        )
    return link_url(settings.web, token)


async def revoke_owner_links(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, now: datetime
) -> int:
    """Revoke every owner link nobody has opened; how many were revoked."""
    async with sessionmaker.begin() as db:
        revoked = await web_invites.revoke_open_links(
            db, tenant_id=tenant_id, kind="owner", now=now
        )
        if revoked:
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=None,
                action="Revoked owner link",
                target="Owner link",
                detail=f"{revoked} unopened",
                at=now,
            )
    return revoked


async def owner_link(settings: Settings, argv: list[str]) -> str:
    """The link, for the workspace named."""
    if settings.web is None:
        raise SystemExit("The web app is not configured.")
    args = parse(argv)
    tenant_id: uuid.UUID = args.workspace
    engine = owner_link_engine(settings)
    try:
        factory = build_session_factory(
            engine,
            crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys),
            allow_plaintext=settings.crypto.allow_plaintext,
        )
        now = datetime.now(UTC)
        if args.revoke:
            revoked = await revoke_owner_links(factory, tenant_id=tenant_id, now=now)
            return f"Revoked {revoked} owner link(s)."
        return await make_owner_link(
            factory, settings, tenant_id=tenant_id, days=args.days, now=now
        )
    finally:
        await engine.dispose()
