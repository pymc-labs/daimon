"""_admit is the identity-taking core of the admission gate."""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._ctx import _admit  # pyright: ignore[reportPrivateUsage]
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_account, make_channel_budget, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_admit_denies_when_over_balance(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, platform_user_id="u1"
    )
    with (
        patch("daimon.adapters.mcp.tools._ctx.is_over_balance", new=AsyncMock(return_value=True)),
        pytest.raises(ToolError, match="credit is depleted"),
    ):
        await _admit(auth, sessionmaker=db_session_factory, billing_config=None, tool_name="ask")


async def test_admit_skips_billing_for_internal_identity() -> None:
    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER)
    with patch(
        "daimon.adapters.mcp.tools._ctx.is_over_balance",
        new=AsyncMock(side_effect=AssertionError("must not check")),
    ):
        result = await _admit(auth, sessionmaker=AsyncMock(), billing_config=None, tool_name="ask")
    assert result is auth


async def _member(
    db_session: AsyncSession, *, policy: TenantAccessPolicy | None, role: Role = Role.USER
) -> AuthIdentity:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await set_role(db_session, account.id, role)
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    return AuthIdentity(
        account_id=account.id, tenant_id=tenant.id, role=role, platform_user_id="u1"
    )


_BALANCE_OK = patch(
    "daimon.adapters.mcp.tools._ctx.is_over_balance", new=AsyncMock(return_value=False)
)


@pytest.mark.parametrize(
    ("policy", "role"),
    [
        (None, Role.USER),
        (TenantAccessPolicy(invoker_user_ids=("u1",)), Role.USER),
        (TenantAccessPolicy(invoker_user_ids=("staff",)), Role.ADMIN),
    ],
    ids=["no-policy-row", "allowlisted", "stored-admin"],
)
async def test_admit_passes_whoever_the_invoker_policy_allows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy | None,
    role: Role,
) -> None:
    auth = await _member(db_session, policy=policy, role=role)
    with _BALANCE_OK:
        result = await _admit(
            auth, sessionmaker=db_session_factory, billing_config=None, tool_name="ask"
        )
    assert result is auth, "an allowed caller must pass through to the billing gates"


async def test_admit_refuses_a_member_outside_the_allowlist_before_billing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _member(db_session, policy=TenantAccessPolicy(invoker_user_ids=("staff",)))
    with (
        patch(
            "daimon.adapters.mcp.tools._ctx.is_over_balance",
            new=AsyncMock(side_effect=AssertionError("policy runs before billing")),
        ),
        pytest.raises(ToolError, match="TERMINAL ERROR: You aren't on"),
    ):
        await _admit(auth, sessionmaker=db_session_factory, billing_config=None, tool_name="ask")


async def test_admit_refuses_when_the_policy_is_unreadable(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _member(db_session, policy=None)
    await db_session.execute(
        text("INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null'::jsonb)"),
        {"t": auth.tenant_id},
    )
    await db_session.commit()
    with _BALANCE_OK, pytest.raises(ToolError, match="TERMINAL ERROR: this workspace's access"):
        await _admit(auth, sessionmaker=db_session_factory, billing_config=None, tool_name="ask")


_PINNED = TenantAccessPolicy(agent_channel_pins={"acme-project": ("c-acme",)})


@pytest.mark.parametrize("role", [Role.USER, Role.ADMIN])
async def test_admit_refuses_a_turn_on_a_pinned_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], role: Role
) -> None:
    """An MCP turn runs in no channel, so it is outside every pin; admins too."""
    auth = await _member(db_session, policy=_PINNED, role=role)

    async def names() -> tuple[str | None, ...]:
        return ("acme-project", None)

    with (
        patch(
            "daimon.adapters.mcp.tools._ctx.is_over_balance",
            new=AsyncMock(side_effect=AssertionError("the pin runs before billing")),
        ),
        pytest.raises(ToolError, match="TERMINAL ERROR: An operator pinned this agent"),
    ):
        await _admit(
            auth,
            sessionmaker=db_session_factory,
            billing_config=None,
            tool_name="start_turn",
            agent_names=names,
        )


async def test_admit_passes_an_unpinned_agent_on_a_tenant_with_pins(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _member(db_session, policy=_PINNED)

    async def names() -> tuple[str | None, ...]:
        return ("clientb-project", "clientb-project")

    with _BALANCE_OK:
        result = await _admit(
            auth,
            sessionmaker=db_session_factory,
            billing_config=None,
            tool_name="ask",
            agent_names=names,
        )
    assert result is auth


async def test_admit_skips_the_agent_lookup_when_nothing_is_pinned(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _member(db_session, policy=None)
    names = AsyncMock(side_effect=AssertionError("no pin, no lookup"))
    with _BALANCE_OK:
        result = await _admit(
            auth,
            sessionmaker=db_session_factory,
            billing_config=None,
            tool_name="ask",
            agent_names=names,
        )
    assert result is auth


def _key(auth: AuthIdentity, *, bound: str | None) -> AuthIdentity:
    return dataclasses.replace(auth, agent_id=uuid.uuid4(), bound_channel_id=bound)


async def _pinned_names() -> tuple[str | None, ...]:
    return ("acme-project", None)


@pytest.mark.parametrize(("bound", "admitted"), [("c-acme", True), ("c-other", False)])
async def test_admit_runs_a_bound_key_in_its_channel_under_the_pin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    bound: str,
    admitted: bool,
) -> None:
    auth = _key(await _member(db_session, policy=_PINNED), bound=bound)
    with _BALANCE_OK:
        if admitted:
            result = await _admit(
                auth,
                sessionmaker=db_session_factory,
                billing_config=None,
                tool_name="start_turn",
                agent_names=_pinned_names,
            )
            assert result is auth, "a key bound inside the pin runs there"
            return
        with pytest.raises(ToolError, match="TERMINAL ERROR: An operator pinned"):
            await _admit(
                auth,
                sessionmaker=db_session_factory,
                billing_config=None,
                tool_name="start_turn",
                agent_names=_pinned_names,
            )


async def test_admit_refuses_a_bound_key_over_its_channel_budget(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    auth = await _member(db_session, policy=None)
    tenant = await get_tenant(db_session, auth.tenant_id)
    assert tenant is not None
    await make_channel_budget(db_session, tenant=tenant, channel_id="c-1", limit_usd=Decimal("0"))
    await db_session.commit()
    bound = dataclasses.replace(_key(auth, bound="c-1"), platform=tenant.platform)
    with _BALANCE_OK, pytest.raises(ToolError, match="This channel has used its spending budget"):
        await _admit(bound, sessionmaker=db_session_factory, billing_config=None, tool_name="ask")
    unbound = dataclasses.replace(_key(auth, bound=None), platform=tenant.platform)
    with _BALANCE_OK:
        result = await _admit(
            unbound, sessionmaker=db_session_factory, billing_config=None, tool_name="ask"
        )
    assert result is unbound, "an unbound key runs in no channel, so no channel budget gates it"
