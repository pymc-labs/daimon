"""set_channel_protection: who may protect or seal a channel, and what it may never lift."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.channel_protection import (
    _set_channel_protection_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import access_policy
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOM = "111111111111111111"
OTHER = "222222222222222222"
ISOLATED = "333333333333333333"


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, McpRuntime]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                sealed_channel_ids=(ISOLATED,),
                isolated_channel_ids=(ISOLATED,),
                agent_channel_pins={"own": (ISOLATED,)},
            ),
        )
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(make_fake_ma_handler(FakeMAState())),
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )
    return tenant.id, account.id, runtime


def _auth(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    admin: bool = False,
    administers: frozenset[str] = frozenset(),
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform="discord",
        platform_user_id="444444444444444444",
        is_admin=admin,
        administered_channel_ids=administers,
    )


async def _policy(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> TenantAccessPolicy:
    async with sessionmaker() as session:
        return await load_access_policy(session, tenant_id=tenant_id)


async def test_a_server_admin_protects_seals_and_lifts_both(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    admin = _auth(tenant_id, account_id, admin=True)

    done = await _set_channel_protection_impl(
        runtime, admin, channel_id=ROOM, protected=True, sealed=True
    )
    assert (done.protected, done.sealed, done.changed) == (True, True, True), done
    again = await _set_channel_protection_impl(runtime, admin, channel_id=ROOM, protected=True)
    assert not again.changed, "repeating the call changes nothing"
    policy = await _policy(committing_sessionmaker, tenant_id)
    assert ROOM in policy.protected_channel_ids and ROOM in policy.sealed_channel_ids

    lifted = await _set_channel_protection_impl(
        runtime, admin, channel_id=ROOM, protected=False, sealed=False
    )
    assert (lifted.protected, lifted.sealed) == (False, False), lifted
    policy = await _policy(committing_sessionmaker, tenant_id)
    assert policy.sealed_channel_ids == (ISOLATED,), "other channels' seals are kept"
    assert policy.protected_channel_ids == (), "the protection is lifted"


async def test_a_channel_admin_neither_protects_nor_seals_their_own_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    channel_admin = _auth(tenant_id, account_id, administers=frozenset({ROOM, ISOLATED}))

    for change in ({"protected": True}, {"sealed": True}, {"protected": False}):
        with pytest.raises(ToolError, match="needs a server admin"):
            await _set_channel_protection_impl(runtime, channel_admin, channel_id=ROOM, **change)
    with pytest.raises(ToolError, match="needs a server admin"):
        await _set_channel_protection_impl(
            runtime, channel_admin, channel_id=ISOLATED, sealed=False
        )
    with pytest.raises(ToolError, match="needs a server admin"):
        await _set_channel_protection_impl(
            runtime, _auth(tenant_id, account_id), channel_id=ROOM, protected=True
        )
    policy = await _policy(committing_sessionmaker, tenant_id)
    assert policy.sealed_channel_ids == (ISOLATED,), "refusals write nothing"
    assert policy.protected_channel_ids == (), "refusals protect nothing"


async def test_an_isolated_channel_stays_sealed_until_its_isolation_ends(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    admin = _auth(tenant_id, account_id, admin=True)
    with pytest.raises(ToolError, match="unmark it confidential first"):
        await _set_channel_protection_impl(runtime, admin, channel_id=ISOLATED, sealed=False)
    protected = await _set_channel_protection_impl(
        runtime, admin, channel_id=ISOLATED, protected=True
    )
    assert protected.protected and protected.sealed, "protecting it keeps the seal"
    with pytest.raises(ToolError, match="Pass protected, sealed or both"):
        await _set_channel_protection_impl(runtime, admin, channel_id=ROOM)


async def test_a_protection_change_waits_out_a_held_policy_fence(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A channel tidy holds the shared policy fence; the write waits instead of failing busy."""
    tenant_id, account_id, runtime = await _world(committing_sessionmaker)
    admin = _auth(tenant_id, account_id, admin=True)
    async with committing_sessionmaker.begin() as holder:
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock_shared("
                "hashtextextended(current_schema() || ':' || :key, 0))"
            ),
            {"key": access_policy._policy_write_key(tenant_id)},  # pyright: ignore[reportPrivateUsage]
        )
        writer = asyncio.create_task(
            _set_channel_protection_impl(runtime, admin, channel_id=ROOM, protected=True)
        )
        await asyncio.sleep(0.3)
        assert not writer.done(), "the write must wait for the fence, not fail as busy"
    done = await asyncio.wait_for(writer, 10)
    assert (done.protected, done.changed) == (True, True), done
