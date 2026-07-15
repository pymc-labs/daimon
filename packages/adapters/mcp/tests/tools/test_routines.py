"""DB-backed unit tests for routines MCP tools.

Each test builds a McpRuntime with a real sessionmaker and calls the private
_*_impl functions directly (no FastMCP Context). Covers happy path, scope
isolation, validation errors, and PATCH update semantics.

Phase 38-06: ``create_routine`` / ``update_routine`` now resolve a daimon-tag
``agent_name`` to a live MA ``agent_id`` at the tool boundary. Tests wire a
real ``AsyncAnthropic`` over ``MARouter`` (transport-level fake — never
``AsyncMock`` on ``client.beta.*``, per guideline:testing).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.routines as _routines_mod
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_managed_agents_agent import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_create_routine_impl = _routines_mod._create_routine_impl  # pyright: ignore[reportPrivateUsage]
_delete_routine_impl = _routines_mod._delete_routine_impl  # pyright: ignore[reportPrivateUsage]
_get_routine_impl = _routines_mod._get_routine_impl  # pyright: ignore[reportPrivateUsage]
_list_routines_impl = _routines_mod._list_routines_impl  # pyright: ignore[reportPrivateUsage]
_update_routine_impl = _routines_mod._update_routine_impl  # pyright: ignore[reportPrivateUsage]
_require_platform_user_id = _routines_mod._require_platform_user_id  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.asyncio


def _ma_agent(*, agent_id: str, name: str, tenant_id: uuid.UUID) -> dict[str, object]:
    """Construct a real ``BetaManagedAgentsAgent`` payload tagged for ``tenant_id``.

    Inline at the call site per guideline:testing — no factory indirection.
    """
    agent = BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: name,
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at="2026-05-19T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-19T00:00:00Z",  # type: ignore[arg-type]
        archived_at=None,
        description=None,
    )
    return agent.model_dump(mode="json")


def _ma_client_with_agents(agents: list[dict[str, object]]) -> AsyncAnthropic:
    """Build a fake AsyncAnthropic whose ``agents.list`` returns ``agents``.

    Transport-level fake (httpx.MockTransport via MARouter) — never AsyncMock
    on ``client.beta.*``.
    """
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response(agents))
    return build_fake_anthropic(router.dispatch)


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    client: AsyncAnthropic | None = None,
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=client if client is not None else MagicMock(),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


def _auth_identity(
    *,
    platform: str | None = "discord",
    external_id: str | None = "g_test",
    platform_user_id: str | None = "u_test",
    tenant_id: uuid.UUID | None = None,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id=external_id,
        platform_user_id=platform_user_id,
    )


async def test_create_routine_stamps_tenant_id_from_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    tenant_id = tenant.id
    await (
        db_session.commit()
    )  # impl opens its own tx via committing_sessionmaker; FK must be committed
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant_id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(platform="discord", external_id="guild_create", tenant_id=tenant_id)
    row = await _create_routine_impl(
        runtime,
        auth,
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone="UTC",
        trigger_message="hi",
        enabled=True,
    )
    assert row.tenant_id == tenant_id, "tenant_id must be stamped from the auth token"


async def test_create_routine_computes_next_fire_at_before_insert(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    tenant_id = tenant.id
    await (
        db_session.commit()
    )  # impl opens its own tx via committing_sessionmaker; FK must be committed
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant_id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant_id)
    before = datetime.now(UTC)
    row = await _create_routine_impl(
        runtime,
        auth,
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone="UTC",
        trigger_message="hi",
        enabled=True,
    )
    assert row.next_fire_at is not None, "next_fire_at must be computed before insert"
    assert row.next_fire_at > before, "next_fire_at must be in the future relative to creation time"


async def test_create_routine_raises_on_invalid_timezone(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Bad timezone is rejected before the MA lookup; a stub client is unnecessary.
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="unknown timezone"):
        await _create_routine_impl(
            runtime,
            auth,
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone="Mars/Phobos",
            trigger_message="hi",
            enabled=True,
        )


async def test_create_routine_raises_on_invalid_cron(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="invalid cron"):
        await _create_routine_impl(
            runtime,
            auth,
            agent_name="daimon",
            cron_expr="not a cron",
            timezone="UTC",
            trigger_message="hi",
            enabled=True,
        )


async def test_list_routines_returns_only_caller_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    # Create one routine per tenant
    await create_routine(
        db_session,
        tenant_id=tenant_a.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="tenant_a_routine",
    )
    await create_routine(
        db_session,
        tenant_id=tenant_b.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="tenant_b_routine",
    )
    await db_session.flush()

    runtime = _runtime(sessionmaker)
    auth = _auth_identity(tenant_id=tenant_a.id)
    rows = await _list_routines_impl(runtime, auth)

    assert len(rows) == 1, "list must return only routines in the caller's tenant"
    assert rows[0].trigger_message == "tenant_a_routine", "only tenant_a's routine must be listed"


async def test_get_routine_returns_row_in_same_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="fetchable",
    )
    await db_session.flush()

    runtime = _runtime(sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id)
    row = await _get_routine_impl(runtime, auth, routine_id=created.id)

    assert row.id == created.id, "get must return the correct row"
    assert row.trigger_message == "fetchable", "row content must match what was created"


async def test_get_routine_raises_routine_not_found_for_cross_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant_a.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="tenant_a_only",
    )
    await db_session.flush()

    runtime = _runtime(sessionmaker)
    auth = _auth_identity(tenant_id=tenant_b.id)
    with pytest.raises(ToolError, match="routine not found"):
        await _get_routine_impl(runtime, auth, routine_id=created.id)


async def test_get_routine_raises_routine_not_found_for_missing_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(sessionmaker)
    auth = _auth_identity()
    with pytest.raises(ToolError, match="routine not found"):
        await _get_routine_impl(runtime, auth, routine_id=uuid.uuid4())


async def test_update_routine_patches_only_provided_fields(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="orig",
        enabled=True,
        next_fire_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id)
    updated = await _update_routine_impl(runtime, auth, routine_id=created.id, enabled=False)

    assert updated.enabled is False, "enabled must be updated to False"
    assert updated.cron_expr == "0 9 * * *", "cron_expr must remain unchanged"
    assert updated.trigger_message == "orig", "trigger_message must remain unchanged"
    assert updated.next_fire_at == datetime(2026, 6, 1, 9, 0, tzinfo=UTC), (
        "next_fire_at must not be recomputed when only enabled changes (D-12)"
    )


async def test_update_routine_recomputes_next_fire_at_when_cron_changes(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    original_fire = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="orig",
        next_fire_at=original_fire,
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id)
    updated = await _update_routine_impl(
        runtime, auth, routine_id=created.id, cron_expr="0 10 * * *"
    )

    assert updated.next_fire_at is not None, "next_fire_at must be set after cron update"
    assert updated.next_fire_at != original_fire, (
        "next_fire_at must be recomputed when cron_expr changes (D-12)"
    )
    assert updated.cron_expr == "0 10 * * *", "new cron_expr must be persisted"


async def test_update_routine_recomputes_next_fire_at_when_timezone_changes(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    original_fire = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="orig",
        next_fire_at=original_fire,
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id)
    updated = await _update_routine_impl(
        runtime, auth, routine_id=created.id, timezone="America/New_York"
    )

    assert updated.next_fire_at is not None, "next_fire_at must be set after timezone update"
    assert updated.next_fire_at != original_fire, (
        "next_fire_at must be recomputed when timezone changes (D-12)"
    )
    assert updated.timezone == "America/New_York", "new timezone must be persisted"


async def test_update_routine_raises_for_cross_tenant(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_owner = await make_tenant(db_session)
    tenant_intruder = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant_owner.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="private",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_intruder.id)
    with pytest.raises(ToolError, match="routine not found"):
        await _update_routine_impl(runtime, auth, routine_id=created.id, trigger_message="hacked")


async def test_delete_routine_removes_row(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="deleteme",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id)
    result = await _delete_routine_impl(runtime, auth, routine_id=created.id)

    assert result.deleted is True, "delete must return deleted=True on success"
    assert result.routine_id == str(created.id), "returned routine_id must match deleted row"

    # Verify deletion is visible via the same runtime (committed, separate connection)
    with pytest.raises(ToolError, match="routine not found"):
        await _get_routine_impl(runtime, auth, routine_id=created.id)


async def test_delete_routine_raises_for_cross_tenant(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_owner = await make_tenant(db_session)
    tenant_intruder = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant_owner.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="protected",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant_intruder.id)
    with pytest.raises(ToolError, match="routine not found"):
        await _delete_routine_impl(runtime, auth, routine_id=created.id)

    # Original row must survive the failed cross-tenant delete
    owner_auth = _auth_identity(tenant_id=tenant_owner.id)
    row = await _get_routine_impl(runtime, owner_auth, routine_id=created.id)
    assert row.id == created.id, "row must survive a failed cross-tenant delete"


_EXPECTED_PLATFORM_USER_ID_ERROR = "creating a routine requires a platform user identity"


async def test_create_routine_stamps_created_by_user_id_from_auth_platform_user_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    tenant_id = tenant.id
    await (
        db_session.commit()
    )  # impl opens its own tx via committing_sessionmaker; FK must be committed
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant_id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(
        platform="discord",
        external_id="g_create",
        platform_user_id="discord_user_42",
        tenant_id=tenant_id,
    )
    row = await _create_routine_impl(
        runtime,
        auth,
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone="UTC",
        trigger_message="hi",
        enabled=True,
    )
    assert row.created_by_user_id == "discord_user_42", (
        "created_by_user_id must be stamped from auth.platform_user_id so the scheduler can later "
        "build a principal and fire the routine"
    )


async def test_require_platform_user_id_raises_with_exact_error_string_when_missing() -> None:
    auth = _auth_identity(platform_user_id=None)
    with pytest.raises(ToolError) as exc_info:
        _require_platform_user_id(auth)
    assert str(exc_info.value) == _EXPECTED_PLATFORM_USER_ID_ERROR, (
        "missing platform_user_id (e.g. CLI session) must be rejected at create_routine "
        "so the scheduler never sees an unfireable row"
    )


# ---------------------------------------------------------------------------
# Phase 38-06: agent_name resolution at the MCP tool boundary.
# ---------------------------------------------------------------------------


async def test_create_routine_resolves_agent_name_to_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """Tool input ``agent_name`` is resolved via find_agent_by_daimon_tag and
    the resolved id is persisted alongside the name."""
    tenant = await make_tenant(db_session)
    tenant_id = tenant.id
    await (
        db_session.commit()
    )  # impl opens its own tx via committing_sessionmaker; FK must be committed
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant_id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(platform="discord", external_id="g_resolve", tenant_id=tenant_id)
    row = await _create_routine_impl(
        runtime,
        auth,
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone="UTC",
        trigger_message="hi",
        enabled=True,
    )
    assert row.agent_id == "ag_resolved", (
        "the resolved MA agent id must be persisted on the row (Phase 38-06 boundary resolution)"
    )
    assert row.agent_name == "daimon", "the tag must be persisted for later re-resolution"


async def test_create_routine_unknown_agent_raises_toolerror(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """When find_agent_by_daimon_tag returns no match, the tool raises ToolError."""
    tenant_id = uuid.uuid4()
    # MA list returns no agents for this tenant.
    client = _ma_client_with_agents([])
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(platform="discord", external_id="g_missing", tenant_id=tenant_id)
    with pytest.raises(ToolError, match="no agent named"):
        await _create_routine_impl(
            runtime,
            auth,
            agent_name="ghost",
            cron_expr="* * * * *",
            timezone="UTC",
            trigger_message="hi",
            enabled=True,
        )


async def test_update_routine_renames_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """Updating with a new ``agent_name`` re-resolves the id and persists both."""
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="ag_original",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()

    # The MA agent must be tagged with the same tenant_id as the auth token
    # so find_agent_by_daimon_tag can match it.
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_other", name="other", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    updated = await _update_routine_impl(runtime, auth, routine_id=created.id, agent_name="other")
    assert updated.agent_name == "other", "new agent_name must be persisted"
    assert updated.agent_id == "ag_other", (
        "agent_id must be re-resolved to the new tag's live MA id"
    )


async def test_update_routine_unknown_agent_raises_toolerror(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    """Updating with an unresolvable ``agent_name`` raises ToolError."""
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="ag_original",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()

    client = _ma_client_with_agents([])  # no agents in MA for this tenant
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id)
    with pytest.raises(ToolError, match="no agent named"):
        await _update_routine_impl(runtime, auth, routine_id=created.id, agent_name="ghost")
