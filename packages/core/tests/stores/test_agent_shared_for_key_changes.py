"""`is_agent_shared_for_key_changes`: who else a key replacement or removal reaches.

Checked against every name the agent answers to, by stable MA id for thread
bindings, and including personal defaults — a display/routing name mismatch
must never make a shared agent look private.
"""

from __future__ import annotations

import pytest
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, UserScopeRef
from daimon.core.stores.scoped_config_read import is_agent_shared_for_key_changes
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NAMES = ("Acme Display", "acme")
_MA_ID = "ag_acme"


async def _shared(db_session: AsyncSession, tenant_id: object, **kw: object) -> bool:
    return await is_agent_shared_for_key_changes(
        db_session,
        tenant_id=tenant_id,  # pyright: ignore[reportArgumentType]
        agent_names=kw.get("names", _NAMES),  # pyright: ignore[reportArgumentType]
        ma_agent_id=_MA_ID,
        default=kw.get("default", DeploymentDefault()),  # pyright: ignore[reportArgumentType]
    )


@pytest.mark.asyncio
async def test_an_unconfigured_agent_is_private(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    assert not await _shared(db_session, tenant.id)


@pytest.mark.asyncio
async def test_a_channel_default_under_the_routing_name_is_shared(db_session: AsyncSession) -> None:
    """The display name is 'Acme Display', the channel names 'acme': still shared."""
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-acme"),
        tenant_id=tenant.id,
        agent_name="acme",
    )
    assert await _shared(db_session, tenant.id)
    assert not await _shared(db_session, tenant.id, names=("Acme Display",)), (
        "control: the display name alone misses it, which was the bypass"
    )


@pytest.mark.asyncio
async def test_a_deployment_default_under_either_name_is_shared(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    assert await _shared(db_session, tenant.id, default=DeploymentDefault(agent_name="acme"))


@pytest.mark.asyncio
async def test_a_live_handoff_binding_is_shared(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="chan-1",
        thread_id="thread-1",
        responder_ma_agent_id=_MA_ID,
        responder_name="whatever-it-was-called",
        kind="handoff",
    )
    assert await _shared(db_session, tenant.id), "bound by stable id, whatever the name"


@pytest.mark.asyncio
async def test_a_personal_default_is_shared(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=account.id),
        tenant_id=tenant.id,
        agent_name="acme",
    )
    assert await _shared(db_session, tenant.id)


@pytest.mark.asyncio
async def test_no_name_at_all_fails_closed(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    assert await _shared(db_session, tenant.id, names=("", ""))
