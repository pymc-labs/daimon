"""Real-Postgres tests for agent_setup/write.py.

Covers:
- do_propagate persists agent_name at the scope (set_fields); second call returns prior name
- do_unpropagate clears the agent_name (unset_fields)
- resolve_account_display renders <@U…> for an account with a Slack principal
- resolve_account_display falls back to account {first8} when no Slack principal exists
- mask_tail covers the full-length and short-string cases
"""

from __future__ import annotations

import uuid

import pytest
from daimon.adapters.slack.agent_setup.write import (
    PropagateResult,
    do_propagate,
    do_unpropagate,
    mask_tail,
    resolve_account_display,
)
from daimon.core._models import Account, Tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import TenantScopeRef
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import get_scope
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_WRITE_TESTS"
_AGENT_NAME = "my-agent"
_OTHER_AGENT_NAME = "other-agent"
_CHANNEL_ID = "C_WRITE_TESTS"


async def _seed_tenant(session: AsyncSession, team_id: str = _TEAM_ID) -> uuid.UUID:
    """Create a Tenant row and return the derived tenant_id."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    session.add(Tenant(id=tenant_id, platform="slack", external_id=team_id))
    await session.flush()
    return tenant_id


async def _seed_account(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Create an Account row and return its id."""
    account = Account(tenant_id=tenant_id, role="user")
    session.add(account)
    await session.flush()
    return account.id  # type: ignore[return-value]  # SA mapped column UUID


# ---------------------------------------------------------------------------
# mask_tail (pure, no DB)
# ---------------------------------------------------------------------------


def test_mask_tail_returns_last4_chars_when_secret_is_long_enough() -> None:
    result = mask_tail("ghp_abcd1234")
    assert result == "****1234", "mask_tail should render ****<last4> for secrets >= 4 chars"


def test_mask_tail_returns_four_stars_when_secret_is_shorter_than_four_chars() -> None:
    result = mask_tail("xy")
    assert result == "****", "mask_tail should return **** for secrets shorter than 4 chars"


def test_mask_tail_returns_four_stars_for_empty_string() -> None:
    result = mask_tail("")
    assert result == "****", "mask_tail should return **** for empty string"


# ---------------------------------------------------------------------------
# do_propagate — persists scope write and returns prior state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_propagate_persists_agent_name_at_tenant_scope(
    db_session: AsyncSession,
) -> None:
    """do_propagate stamps agent_name at TenantScopeRef; get_scope shows the persisted value."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)

    scope = TenantScopeRef(tenant_id=tenant_id)
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        actor_account_id=account_id,
    )

    assert isinstance(result, PropagateResult), "do_propagate should return PropagateResult"
    assert result.prior_agent_name is None, "clean propagation should have no prior agent name"

    row = await get_scope(db_session, scope=scope)
    from daimon.core.scope import TenantConfigRow

    assert isinstance(row, TenantConfigRow), (
        "get_scope should return a TenantConfigRow after propagate"
    )
    assert row.agent_name == _AGENT_NAME, "propagated agent_name should be persisted at the scope"
    assert row.agent_name_set_by_account_id == account_id, (
        "actor account_id should be recorded for audit"
    )


@pytest.mark.asyncio
async def test_do_propagate_returns_prior_agent_name_on_overwrite(
    db_session: AsyncSession,
) -> None:
    """Second do_propagate returns the prior agent name (last-write-wins audit trail)."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)
    second_account_id = await _seed_account(db_session, tenant_id)

    scope = TenantScopeRef(tenant_id=tenant_id)

    # First propagation — clean write
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        actor_account_id=account_id,
    )

    # Second propagation — overwrite; prior name should surface
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_OTHER_AGENT_NAME,
        actor_account_id=second_account_id,
    )

    assert result.prior_agent_name == _AGENT_NAME, (
        "do_propagate should return the agent_name that was overwritten"
    )
    assert result.prior_actor_account_id == account_id, (
        "do_propagate should return the actor who set the prior value"
    )


# ---------------------------------------------------------------------------
# do_unpropagate — clears agent_name at scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_unpropagate_clears_agent_name_at_scope(
    db_session: AsyncSession,
) -> None:
    """do_unpropagate removes agent_name so the row becomes effectively empty."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)

    scope = TenantScopeRef(tenant_id=tenant_id)
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        actor_account_id=account_id,
    )

    await do_unpropagate(db_session, scope=scope, actor_account_id=account_id)

    row = await get_scope(db_session, scope=scope)
    from daimon.core.scope import TenantConfigRow

    # After unpropagate, either the row is gone (None) or agent_name is None
    if isinstance(row, TenantConfigRow):
        assert row.agent_name is None, "do_unpropagate should clear agent_name from the scope row"
    else:
        assert row is None, (
            "do_unpropagate should leave the scope empty (row deleted or agent_name None)"
        )


# ---------------------------------------------------------------------------
# resolve_account_display — audit display with Slack principal join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_account_display_returns_slack_mention_when_principal_exists(
    db_session: AsyncSession,
) -> None:
    """resolve_account_display returns <@U…> for an account with a Slack principal."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)

    slack_user_id = "U01TESTSLACK"
    await get_or_create_platform_principal(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        external_id=slack_user_id,
    )
    # The above creates a new account; we need one linked to our account.
    # Use the identity store directly for the principal tied to our account_id.
    from daimon.core._models import PlatformPrincipal

    existing_principal = PlatformPrincipal(
        tenant_id=tenant_id,
        platform="slack",
        external_id=f"U_UNIQUE_{account_id.hex[:8]}",
        account_id=account_id,
    )
    db_session.add(existing_principal)
    await db_session.flush()

    result = await resolve_account_display(db_session, account_id=account_id)
    expected_mention = f"<@U_UNIQUE_{account_id.hex[:8]}>"
    assert result == expected_mention, (
        "resolve_account_display should render <@U…> for an account with a Slack principal"
    )


@pytest.mark.asyncio
async def test_resolve_account_display_falls_back_to_account_prefix_when_no_principal(
    db_session: AsyncSession,
) -> None:
    """resolve_account_display returns account {first8} when no Slack principal exists."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)

    result = await resolve_account_display(db_session, account_id=account_id)
    assert result == f"account {str(account_id)[:8]}", (
        "resolve_account_display should fall back to 'account {first8}' when no Slack principal"
    )
