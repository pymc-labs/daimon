"""_admit is the identity-taking core of the admission gate."""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._ctx import (
    _admission_recheck,  # pyright: ignore[reportPrivateUsage]
    _admit,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.turn_outcomes import list_for_tenant
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_account, make_channel_budget, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def _current_member(*, is_admin: bool = False):  # type: ignore[no-untyped-def]
    from daimon.core.membership import CachedMembershipResolver, Membership

    return CachedMembershipResolver(
        AsyncMock(return_value=Membership(is_member=True, is_admin=is_admin))
    )


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
    ids=["no-policy-row", "allowlisted", "live-admin"],
)
async def test_admit_passes_whoever_the_invoker_policy_allows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy | None,
    role: Role,
) -> None:
    auth = await _member(db_session, policy=policy, role=role)
    auth = dataclasses.replace(auth, platform="discord")
    with _BALANCE_OK:
        result = await _admit(
            auth,
            sessionmaker=db_session_factory,
            membership_resolver=_current_member(is_admin=role is Role.ADMIN),
            billing_config=None,
            tool_name="ask",
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
    with _BALANCE_OK, pytest.raises(ToolError, match="TERMINAL ERROR: the access settings here"):
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
        pytest.raises(
            ToolError, match="TERMINAL ERROR: This agent.s rule runs it only in certain channels"
        ),
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
        with pytest.raises(ToolError, match="TERMINAL ERROR: This agent.s rule runs it only"):
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
        await _admit(
            bound,
            membership_resolver=_current_member(is_admin=False),
            sessionmaker=db_session_factory,
            billing_config=None,
            tool_name="ask",
        )
    unbound = dataclasses.replace(_key(auth, bound=None), platform=tenant.platform)
    with _BALANCE_OK:
        result = await _admit(
            unbound,
            membership_resolver=_current_member(is_admin=False),
            sessionmaker=db_session_factory,
            billing_config=None,
            tool_name="ask",
        )
    assert result is unbound, "an unbound key runs in no channel, so no channel budget gates it"


async def test_admission_recheck_refuses_a_bound_key_once_its_pin_moves_away(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The action-time re-check decides with the key's channel too: a pin moved
    off it between admission and the session create stops the turn."""
    auth = _key(await _member(db_session, policy=_PINNED), bound="c-acme")
    recheck = _admission_recheck(
        auth, sessionmaker=db_session_factory, tool_name="start_turn", agent_names=_pinned_names
    )
    await recheck()
    await set_access_policy(
        db_session,
        tenant_id=auth.tenant_id,
        policy=TenantAccessPolicy(agent_channel_pins={"acme-project": ("c-new",)}),
    )
    await db_session.commit()

    with pytest.raises(ToolError, match="TERMINAL ERROR: This agent.s rule runs it only"):
        await recheck()


_ISOLATED = TenantAccessPolicy(
    sealed_channel_ids=("room",),
    isolated_channel_ids=("room",),
    agent_channel_pins={"local": ("room",)},
)


async def test_a_hub_run_of_an_isolated_channels_agent_is_held_to_that_channels_budget(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin = await _member(db_session, policy=_ISOLATED, role=Role.ADMIN)
    tenant = await get_tenant(db_session, admin.tenant_id)
    assert tenant is not None
    await make_channel_budget(db_session, tenant=tenant, channel_id="room", limit_usd=Decimal("0"))
    await db_session.commit()
    auth = dataclasses.replace(admin, platform=tenant.platform)

    async def names(agent: str) -> tuple[str | None, ...]:
        return agent, None

    with _BALANCE_OK, pytest.raises(ToolError, match="This channel has used its spending budget"):
        await _admit(
            auth,
            membership_resolver=_current_member(is_admin=True),
            sessionmaker=db_session_factory,
            billing_config=None,
            tool_name="ask",
            agent_names=lambda: names("local"),
            pin_exempt=True,
        )
    with _BALANCE_OK:
        assert (
            await _admit(
                auth,
                membership_resolver=_current_member(is_admin=True),
                sessionmaker=db_session_factory,
                billing_config=None,
                tool_name="ask",
                agent_names=lambda: names("daimon"),
                pin_exempt=True,
            )
            is auth
        )


@pytest.mark.parametrize(
    ("policy", "bound", "agent", "expected"),
    [
        (_PINNED, None, "acme-project", TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE),
        (_ISOLATED, "room", "daimon", TerminationReason.ADMISSION_CHANNEL_ISOLATED),
    ],
    ids=["pinned", "isolated"],
)
async def test_a_refused_turn_records_which_gate_refused_it(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    policy: TenantAccessPolicy,
    bound: str | None,
    agent: str,
    expected: TerminationReason,
) -> None:
    """`daimon usage turns` tells a pin from an isolation refusal, not just `admission_denied`."""
    auth = _key(await _member(db_session, policy=policy), bound=bound)
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def names() -> tuple[str | None, ...]:
        return (agent, None)

    with pytest.raises(ToolError, match="TERMINAL ERROR"):
        await _admit(
            auth,
            sessionmaker=sessionmaker,
            billing_config=None,
            tool_name="start_turn",
            agent_names=names,
        )
    await drain_outcomes()
    async with sessionmaker() as session:
        rows = await list_for_tenant(session, auth.tenant_id)
    assert [row.reason for row in rows] == [expected], "the refusal is recorded under its gate"


async def test_admit_over_the_cap_says_the_cap_is_the_callers_and_an_operator_raises_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, platform_user_id="u1"
    )
    with (
        _BALANCE_OK,
        patch("daimon.adapters.mcp.tools._ctx.is_over_cap", new=AsyncMock(return_value=True)),
        pytest.raises(ToolError) as refused,
    ):
        await _admit(auth, sessionmaker=db_session_factory, billing_config=None, tool_name="ask")
    assert str(refused.value) == (
        "TERMINAL ERROR: You've used your monthly limit.\n\nAsk the team running Daimon to raise it."
    ), "the MCP refusal uses the shared cap wording"


async def test_mcp_role_gate_uses_resolver_without_gateway_facts(db_session, db_session_factory):  # type: ignore[no-untyped-def]
    from daimon.core.membership import CachedMembershipResolver, Membership

    auth = await _member(db_session, policy=TenantAccessPolicy(invoker_role_ids=("123",)))
    auth = dataclasses.replace(auth, platform="discord")
    facts = [Membership(is_member=True, role_ids=("123",))]

    async def fetch(tenant_id, guild_id, user_id):  # type: ignore[no-untyped-def]
        return facts[0]

    resolver = CachedMembershipResolver(fetch)
    with _BALANCE_OK:
        assert (
            await _admit(
                auth,
                sessionmaker=db_session_factory,
                membership_resolver=resolver,
                billing_config=None,
                tool_name="ask",
            )
            is auth
        )
    facts[0] = Membership(is_member=True)
    tenant = await get_tenant(db_session, auth.tenant_id)
    assert tenant is not None
    resolver.invalidate(auth.tenant_id, tenant.external_id, "u1")
    with pytest.raises(ToolError, match="TERMINAL ERROR"):
        await _admission_recheck(
            auth,
            sessionmaker=db_session_factory,
            membership_resolver=resolver,
            tool_name="ask",
            agent_names=None,
        )()


async def test_private_result_delivery_bypasses_cached_role_grant(db_session, db_session_factory):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from daimon.adapters.mcp.tools.agent_chat import _require_live_result_access
    from daimon.core.membership import CachedMembershipResolver, Membership

    auth = dataclasses.replace(
        await _member(db_session, policy=TenantAccessPolicy(invoker_role_ids=("123",))),
        platform="discord",
    )
    tenant = await get_tenant(db_session, auth.tenant_id)
    assert tenant is not None
    fetch = AsyncMock(return_value=Membership(is_member=True))
    resolver = CachedMembershipResolver(fetch)
    resolver.observe(
        auth.tenant_id, tenant.external_id, "u1", Membership(is_member=True, role_ids=("123",))
    )
    runtime = SimpleNamespace(session_factory=db_session_factory, membership_resolver=resolver)
    with pytest.raises(ToolError, match="TERMINAL ERROR"):
        await _require_live_result_access(runtime, auth)  # type: ignore[arg-type]
    fetch.assert_awaited_once_with(auth.tenant_id, tenant.external_id, "u1")


@pytest.mark.parametrize("policy", [None, TenantAccessPolicy(invoker_user_ids=("u1",))])
async def test_discord_human_without_membership_credentials_is_refused_before_billing(
    db_session, db_session_factory, policy
):  # type: ignore[no-untyped-def]
    auth = dataclasses.replace(await _member(db_session, policy=policy), platform="discord")
    with (
        patch(
            "daimon.adapters.mcp.tools._ctx.is_over_balance",
            new=AsyncMock(
                side_effect=AssertionError("unknown membership must refuse before billing")
            ),
        ),
        pytest.raises(ToolError, match="TERMINAL ERROR"),
    ):
        await _admit(
            auth,
            sessionmaker=db_session_factory,
            membership_resolver=None,
            billing_config=None,
            tool_name="ask",
        )


@pytest.mark.parametrize("pin_exempt", [False, True])
@pytest.mark.parametrize("human", [False, True])
async def test_agent_chat_role_gate_cannot_be_bypassed_by_hub_admin_or_internal_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    pin_exempt: bool,
    human: bool,
) -> None:
    from daimon.core.access_policy import AgentRule
    from daimon.core.membership import CachedMembershipResolver, Membership

    auth = await _member(
        db_session,
        role=Role.ADMIN,
        policy=TenantAccessPolicy(agent_rules={"restricted": AgentRule(invoker_role_ids=("123",))}),
    )
    auth = dataclasses.replace(auth, platform="discord", platform_user_id="u1" if human else None)
    fetch = AsyncMock(return_value=Membership(is_member=True, is_admin=True))
    names = AsyncMock(return_value=("restricted",))
    with pytest.raises(ToolError, match="You aren't on"):
        await _admit(
            auth,
            sessionmaker=db_session_factory,
            membership_resolver=CachedMembershipResolver(fetch),
            billing_config=None,
            tool_name="ask",
            agent_names=names,
            pin_exempt=pin_exempt,
        )
    names.assert_awaited_once()


async def test_agent_chat_role_revoked_before_message_send_denied_without_invalidation(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.access_policy import AgentRule
    from daimon.core.membership import CachedMembershipResolver, Membership

    auth = dataclasses.replace(
        await _member(
            db_session,
            policy=TenantAccessPolicy(
                agent_rules={"restricted": AgentRule(invoker_role_ids=("123",))}
            ),
        ),
        platform="discord",
    )
    fetch = AsyncMock(return_value=Membership(is_member=True, role_ids=("123",)))
    resolver = CachedMembershipResolver(fetch)
    names = AsyncMock(return_value=("restricted",))
    with _BALANCE_OK:
        await _admit(
            auth,
            sessionmaker=db_session_factory,
            membership_resolver=resolver,
            billing_config=None,
            tool_name="ask",
            agent_names=names,
        )
    fetch.return_value = Membership(is_member=True)
    with pytest.raises(ToolError, match="You aren't on"):
        await _admission_recheck(
            auth,
            sessionmaker=db_session_factory,
            membership_resolver=resolver,
            tool_name="ask",
            agent_names=names,
        )()
