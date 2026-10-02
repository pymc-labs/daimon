"""Real-Postgres tests for agent_setup/write.py.

Covers:
- do_propagate persists agent_name at the scope (set_fields); second call returns prior name
- do_unpropagate clears the agent_name (unset_fields)
- mask_tail covers the full-length and short-string cases
- delete_agent archives the memory store via core.agent_lifecycle (mirrors Discord)
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup import write as write_mod
from daimon.adapters.slack.agent_setup.write import (
    PropagateResult,
    do_propagate,
    do_unpropagate,
    load_agent_inline_pat,
    owner_repo_from_url,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_archive_agent_handler,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_WRITE_TESTS"
_AGENT_NAME = "my-agent"
_OTHER_AGENT_NAME = "other-agent"
_CHANNEL_ID = "C_WRITE_TESTS"


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
# load_agent_inline_pat / owner_repo_from_url
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


def test_owner_repo_from_url_collapses_scheme_host_and_git_variants() -> None:
    canonical = "acme-org/widgets"
    variants = [
        "https://github.com/acme-org/widgets",
        "http://github.com/acme-org/widgets",
        "github.com/acme-org/widgets",
        "https://github.com/acme-org/widgets.git",
        "https://github.com/acme-org/widgets/",
        "acme-org/widgets",
    ]
    for variant in variants:
        assert owner_repo_from_url(variant) == canonical, (
            f"owner_repo_from_url({variant!r}) must canonicalize to {canonical!r}"
        )


def test_owner_repo_from_url_matches_store_normalization() -> None:
    """Must stay byte-identical to the store's own normalization — a probe run
    against a differently-canonicalized string would verify a different repo
    than the one the binding actually records."""
    from daimon.core.stores.agent_repo_binding import (
        _normalize_owner_repo,  # pyright: ignore[reportPrivateUsage]
    )

    for url in [
        "https://github.com/acme-org/widgets.git",
        "github.com/acme-org/widgets/",
        "acme-org/widgets",
    ]:
        assert owner_repo_from_url(url) == _normalize_owner_repo(url), (
            "owner_repo_from_url must stay byte-identical to the store's normalization"
        )


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


def _agent_dict(
    *,
    id_: str,
    name: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Build a real BetaManagedAgentsAgent and dump to JSON for the MockTransport."""
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": name,
    }
    if account_id is not None:
        metadata["daimon_account"] = str(account_id)
    return BetaManagedAgentsAgent(
        id=id_,
        type="agent",
        name=name,
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata=metadata,
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")


async def test_delete_agent_archives_memory_store(db_session, db_session_factory) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_MEM")
    mem_state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(
            make_archive_agent_handler(),
            make_fake_memory_store_handler(mem_state),
            make_fake_ma_handler(),
        )
    )

    agent = await client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "doomed"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")

    from daimon.core.stores.agent_memory_stores import get_memory_store_id, insert_memory_store

    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="doomed")

    assert mem_state.stores[store.id]["archived_at"] is not None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_uuid) is None


def _make_failing_store_archive_handler() -> Callable[[httpx.Request], httpx.Response]:
    """500 on memory-store archive — simulates a transient MA outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and re.fullmatch(
            r"/v1/memory_stores/[^/]+/archive", request.url.path
        ):
            return httpx.Response(
                500,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        raise NotHandled

    return handler


async def test_delete_agent_succeeds_when_store_archive_fails(
    db_session, db_session_factory
) -> None:
    """Transient store-archive failure must not fail agent deletion (best-effort degrade)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_FAIL")
    mem_state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(
            make_archive_agent_handler(),
            _make_failing_store_archive_handler(),
            make_fake_memory_store_handler(mem_state),
            make_fake_ma_handler(),
        )
    )

    agent = await client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "doomed"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")

    from daimon.core.stores.agent_memory_stores import get_memory_store_id, insert_memory_store

    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    # Must not raise despite the 500 from the store archive.
    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="doomed")

    assert mem_state.stores[store.id]["archived_at"] is None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_uuid) == store.id


async def test_delete_agent_clears_tenant_default_naming_the_agent(
    db_session, db_session_factory
) -> None:
    """Deleting the workspace default must not leave a tenant row naming a dead agent.

    A tenant_config row pointing at an archived agent breaks resolution for the
    whole install until an admin re-scopes by hand.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_SCOPE")
    scope = TenantScopeRef(tenant_id=tenant.id)
    await set_fields(db_session, scope=scope, tenant_id=tenant.id, agent_name="doomed")
    await db_session.commit()

    client = build_fake_anthropic(
        combine_handlers(
            make_archive_agent_handler(),
            make_fake_memory_store_handler(FakeMemoryStoreState()),
            make_fake_ma_handler(),
        )
    )
    await client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "doomed"},
    )

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="doomed")

    async with db_session_factory() as s:
        assert await get_scope(s, scope=scope) is None, (
            "the tenant row naming the deleted agent must be gone so resolution "
            "falls through to the deployment default"
        )


# ---------------------------------------------------------------------------
# delete_agent — server-side refusal for defaults-managed ("system") agents
# ---------------------------------------------------------------------------


def _make_recording_archive_handler(
    archived_ids: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Archive handler that records which agent ids MA was actually asked to archive.

    Lets a test assert the guard fired *before* the archive call, not merely
    that `delete_agent` raised.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)/archive", request.url.path)
        if request.method != "POST" or not m:
            raise NotHandled
        archived_ids.append(m.group("id"))
        now = datetime.now(UTC)
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id=m.group("id"),
                type="agent",
                name="doomed",
                model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
                metadata={},
                description=None,
                archived_at=now,
                created_at=now,
                updated_at=now,
                version=2,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    return handler


async def test_delete_agent_refuses_defaults_managed_agent(db_session, db_session_factory) -> None:
    """A workspace admin must not be able to archive the deployment's built-in agent.

    The Slack roster carries no is_system flag, so the panel offers Delete for
    seeded agents — the refusal has to live server-side or the deployment's
    agent (and its memory store) go away in two clicks.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_MANAGED")
    archived_ids: list[str] = []
    client = build_fake_anthropic(
        combine_handlers(
            _make_recording_archive_handler(archived_ids),
            make_fake_memory_store_handler(FakeMemoryStoreState()),
            make_fake_ma_handler(),
        )
    )
    await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_name": "daimon",
            "daimon_managed": "true",
        },
    )

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    with pytest.raises(DaimonError, match="built-in agent"):
        await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="daimon")

    assert archived_ids == [], (
        "delete_agent must refuse a daimon_managed agent before calling agents.archive"
    )


async def test_delete_agent_still_archives_unmanaged_agent(db_session, db_session_factory) -> None:
    """The managed guard must not over-block: user forks still delete normally."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_UNMANAGED")
    archived_ids: list[str] = []
    client = build_fake_anthropic(
        combine_handlers(
            _make_recording_archive_handler(archived_ids),
            make_fake_memory_store_handler(FakeMemoryStoreState()),
            make_fake_ma_handler(),
        )
    )
    agent = await client.beta.agents.create(
        name="my-fork",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "my-fork"},
    )

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="my-fork")

    assert archived_ids == [agent.id], (
        "an agent without the daimon_managed marker must still be archived"
    )
