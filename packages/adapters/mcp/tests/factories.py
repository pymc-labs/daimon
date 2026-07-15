"""MCP-adapter-specific test factories.

Domain-row factories (agents, environments, accounts) live in
packages/core/tests/factories.py; don't duplicate them.
"""

from __future__ import annotations

import datetime as dt
import uuid

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core._models import Account
from daimon.core.mcp_auth import mint_jwt
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


def make_jwt(
    *,
    account_id: uuid.UUID,
    secret: bytes = b"a" * 32,
    now: dt.datetime | None = None,
    is_admin: bool = False,
) -> str:
    return mint_jwt(
        account_id=account_id,
        secret=secret,
        now=now or dt.datetime(2026, 4, 24, tzinfo=dt.UTC),
        is_admin=is_admin,
    )


def make_identity(
    *,
    account_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    role: Role = Role.ADMIN,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        role=role,
    )


async def seed_tenant(session: AsyncSession, *, workspace_id: str | None = None) -> uuid.UUID:
    """Insert a Tenant row and return its id."""
    tenant = await make_tenant(
        session, platform="discord", workspace_id=workspace_id or str(uuid.uuid4())
    )
    return tenant.id


async def seed_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    role: str = "user",
) -> uuid.UUID:
    """Insert an Account row belonging to the given tenant and return its id."""
    account = Account(tenant_id=tenant_id, role=role)
    session.add(account)
    await session.flush()
    return account.id


async def seed_tenant_and_account(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Tenant + Account and return (tenant_id, account_id)."""
    tid = await seed_tenant(session)
    aid = await seed_account(session, tenant_id=tid)
    return tid, aid


def make_ma_agent(**overrides: object) -> BetaManagedAgentsAgent:
    base: dict[str, object] = {
        "id": "ag_new",
        "type": "agent",
        "version": 1,
        "name": "demo",
        "model": {"id": "claude-opus-4-5"},
        "description": None,
        "system": None,
        "tools": [],
        "mcp_servers": [],
        "skills": [],
        "created_at": "2026-04-24T00:00:00Z",
        "updated_at": "2026-04-24T00:00:00Z",
        "metadata": {},
    }
    base.update(overrides)
    return BetaManagedAgentsAgent.model_validate(base)
