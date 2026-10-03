"""Real-Postgres tests for agent_setup/write.py.

Covers:
- do_propagate persists agent_name at the scope (set_fields); second call returns prior name
- do_unpropagate clears the agent_name (unset_fields)
- mask_tail covers the full-length and short-string cases
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup import write as write_mod
from daimon.adapters.slack.agent_setup.write import (
    PropagateResult,
    do_propagate,
    do_unpropagate,
    load_agent_inline_pat,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_WRITE_TESTS"
_AGENT_NAME = "my-agent"
_OTHER_AGENT_NAME = "other-agent"


async def _seed_tenant(session: AsyncSession, team_id: str = _TEAM_ID) -> uuid.UUID:
    """Create a Tenant row and return the derived tenant_id."""
    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    return tenant.id


async def _seed_account(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Create an Account row and return its id."""
    tenant_row = await get_tenant(session, tenant_id)
    assert tenant_row is not None, "_seed_account requires a tenant seeded via _seed_tenant"
    account = await make_account(session, tenant=tenant_row)
    return account.id


# ---------------------------------------------------------------------------
# load_agent_inline_pat
# ---------------------------------------------------------------------------


def _runtime_for_inline_pat(
    *,
    sessionmaker: Any,
    fernet_key: str | None,
    fallback_pat: str | None = None,
) -> SlackRuntime:
    """Build a SlackRuntime with just enough settings for load_agent_inline_pat."""
    settings = MagicMock()
    settings.crypto.keys = (
        (MagicMock(get_secret_value=lambda: fernet_key),) if fernet_key is not None else ()
    )
    settings.github.oauth_scopes = ("repo", "read:user")
    settings.github.fallback_pat = (
        MagicMock(get_secret_value=lambda: fallback_pat) if fallback_pat is not None else None
    )
    return SlackRuntime(
        settings=settings,
        anthropic=MagicMock(),
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        deployment_default=DeploymentDefault(),
    )


async def test_load_agent_inline_pat_returns_none_when_crypto_unconfigured() -> None:
    """No crypto keys -> no inline PAT could exist; the sessionmaker must never
    be touched (calling _build_runtime_fernet unconditionally would raise)."""
    agent_id = uuid.uuid4()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sessionmaker must not be called when crypto is unconfigured")

    runtime = _runtime_for_inline_pat(sessionmaker=_boom, fernet_key=None)

    result = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result is None, "no crypto keys configured -> no inline PAT can exist"


async def test_load_agent_inline_pat_returns_stored_pat_for_agent_that_has_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Round-trips through store_inline_pat -> load_agent_inline_pat, decrypted."""
    await make_tenant(db_session, platform="slack", workspace_id="T_INLINE_PAT_LOAD")

    fernet_key = Fernet.generate_key().decode()
    plaintext = "ghp_slack_inline_pat_load_9999"
    runtime = _runtime_for_inline_pat(sessionmaker=db_session_factory, fernet_key=fernet_key)

    agent_id = uuid.uuid4()
    await write_mod.store_inline_pat(
        runtime,
        account_id=uuid.uuid4(),
        agent_id=agent_id,
        plaintext_pat=plaintext,
    )

    result = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result == plaintext, "load_agent_inline_pat must decrypt and return the exact stored PAT"


async def test_load_agent_inline_pat_returns_none_for_agent_with_no_token_even_with_fallback_configured(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """allow_service_default=False must be honored: an agent with no stored
    token of its own gets None, never the deployment's shared fallback PAT —
    this is the check that stops the shared public-read token from being
    treated as the agent's own credential."""
    await make_tenant(db_session, platform="slack", workspace_id="T_INLINE_PAT_NO_FALLBACK")

    fernet_key = Fernet.generate_key().decode()
    runtime = _runtime_for_inline_pat(
        sessionmaker=db_session_factory,
        fernet_key=fernet_key,
        fallback_pat="ghp_shared_operator_fallback",
    )

    result = await load_agent_inline_pat(runtime, agent_id=uuid.uuid4())
    assert result is None, (
        "an agent with no stored token must resolve to None, not the shared fallback PAT"
    )


# Repository-normalization tests were removed with the unused helper.


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
# delete_agent — core.agent_lifecycle repoint (mirrors Discord)
# ---------------------------------------------------------------------------


# Legacy deletion is covered by the core lifecycle tests.
