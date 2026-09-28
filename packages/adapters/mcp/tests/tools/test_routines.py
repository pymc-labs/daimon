"""DB-backed unit tests for routines MCP tools.

Each test builds a McpRuntime with a real sessionmaker and calls the private
_*_impl functions directly (no FastMCP Context). Covers happy path, scope
isolation, validation errors, and PATCH update semantics.

``create_routine`` / ``update_routine`` now resolve a daimon-tag
``agent_name`` to a live MA ``agent_id`` at the tool boundary. Tests wire a
real ``AsyncAnthropic`` over ``MARouter`` (transport-level fake — never
``AsyncMock`` on ``client.beta.*``, per guideline:testing).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.routines as _routines_mod
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing import ma_agent, ma_model_config
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


def _ma_agent(*, agent_id: str, name: str, tenant_id: uuid.UUID) -> dict[str, object]:
    """Construct a real ``BetaManagedAgentsAgent`` payload tagged for ``tenant_id``.

    Inline at the call site per guideline:testing — no factory indirection.
    """
    agent = ma_agent(
        id=agent_id,
        name=name,
        model=ma_model_config("claude-sonnet-4-6", speed="standard"),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: name,
        },
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
    is_admin: bool = False,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id=external_id,
        platform_user_id=platform_user_id,
        is_admin=is_admin,
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
        created_by_user_id="u_test",
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
        "next_fire_at must not be recomputed when only enabled changes"
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
        created_by_user_id="u_test",
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
        "next_fire_at must be recomputed when cron_expr changes"
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
        created_by_user_id="u_test",
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
        "next_fire_at must be recomputed when timezone changes"
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
        created_by_user_id="u_test",
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
# agent_name resolution at the MCP tool boundary.
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
        "the resolved MA agent id must be persisted on the row (boundary resolution)"
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
        created_by_user_id="u_test",
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
        created_by_user_id="u_test",
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


# ---------------------------------------------------------------------------
# Owner-or-admin gate on mutation: only the creator or an admin may mutate a
# routine. Reads stay tenant-wide and are unaffected.
# ---------------------------------------------------------------------------


async def test_update_routine_raises_when_caller_is_not_owner(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="U_owner",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id="U_other", is_admin=False)
    with pytest.raises(ToolError, match="routine not found"):
        await _update_routine_impl(runtime, auth, routine_id=created.id, trigger_message="hacked")

    row = await get_routine(db_session, created.id, tenant_id=tenant.id)
    assert row is not None, "row must still exist after a denied update"
    assert row.trigger_message == "orig", (
        "a non-owner's denied update must not change trigger_message"
    )


async def test_delete_routine_raises_when_caller_is_not_owner(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="U_owner",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id="U_other", is_admin=False)
    with pytest.raises(ToolError, match="routine not found"):
        await _delete_routine_impl(runtime, auth, routine_id=created.id)

    row = await get_routine(db_session, created.id, tenant_id=tenant.id)
    assert row is not None, "a non-owner's denied delete must leave the row in place"


async def test_owner_update_and_delete_succeed_when_caller_is_creator(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="U_owner",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id="U_owner", is_admin=False)
    updated = await _update_routine_impl(
        runtime, auth, routine_id=created.id, trigger_message="updated by owner"
    )
    assert updated.trigger_message == "updated by owner", (
        "the creator must be able to update their own routine"
    )

    result = await _delete_routine_impl(runtime, auth, routine_id=created.id)
    assert result.deleted is True, "the creator must be able to delete their own routine"


async def test_update_routine_raises_when_neither_caller_nor_routine_has_a_user_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A missing user id on both sides is not a match — it is two absences."""
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=None,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id=None, is_admin=False)
    with pytest.raises(ToolError, match="routine not found"):
        await _update_routine_impl(runtime, auth, routine_id=created.id, trigger_message="hacked")

    row = await get_routine(db_session, created.id, tenant_id=tenant.id)
    assert row is not None, "row must still exist after a denied update"
    assert row.trigger_message == "orig", (
        "a caller with no platform user id must not be able to rewrite a routine "
        "that has no recorded creator"
    )


async def test_delete_routine_raises_when_neither_caller_nor_routine_has_a_user_id(
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
        trigger_message="orig",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id=None, is_admin=False)
    with pytest.raises(ToolError, match="routine not found"):
        await _delete_routine_impl(runtime, auth, routine_id=created.id)

    row = await get_routine(db_session, created.id, tenant_id=tenant.id)
    assert row is not None, (
        "a caller with no platform user id must not be able to delete a routine "
        "that has no recorded creator"
    )


async def test_admin_update_and_delete_succeed_when_caller_is_non_owner_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="U_owner",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()

    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id="U_other", is_admin=True)
    updated = await _update_routine_impl(
        runtime, auth, routine_id=created.id, trigger_message="updated by admin"
    )
    assert updated.trigger_message == "updated by admin", (
        "an admin must be able to update a routine they did not create"
    )

    result = await _delete_routine_impl(runtime, auth, routine_id=created.id)
    assert result.deleted is True, "an admin must be able to delete a routine they did not create"


async def test_catch_up_policy_create_update_and_owner_gate(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="agent_policy", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(db_session_factory, client=client)
    owner = _auth_identity(tenant_id=tenant.id, platform_user_id="owner")
    row = await _create_routine_impl(
        runtime,
        owner,
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone="UTC",
        trigger_message="run",
        catch_up_policy="run-once",
    )
    assert row.catch_up_policy == "run-once"
    intruder = _auth_identity(tenant_id=tenant.id, platform_user_id="other")
    with pytest.raises(ToolError, match="routine not found"):
        await _update_routine_impl(runtime, intruder, routine_id=row.id, catch_up_policy="skip")
    updated = await _update_routine_impl(runtime, owner, routine_id=row.id, catch_up_policy="skip")
    assert updated.catch_up_policy == "skip"
    assert updated.next_fire_at == row.next_fire_at
    await client.close()


# --- FEAT-085: destination -----------------------------------------------------

_GUILD = "424242"


def _discord_channels(monkeypatch: pytest.MonkeyPatch, channels: dict[int, object]) -> None:
    """Fake the REST client: `fetch_channel` returns from `channels` or 404s."""
    import contextlib

    import discord

    class _Client:
        async def fetch_channel(self, channel_id: int) -> object:
            if channel_id not in channels:
                raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")
            return channels[channel_id]

    @contextlib.asynccontextmanager
    async def fake_rest_client(token: object) -> Any:
        yield _Client()

    monkeypatch.setattr(_routines_mod, "rest_client", fake_rest_client)
    monkeypatch.setattr(_routines_mod, "_require_bot_token", lambda runtime: "token")

    async def fake_resolve_member(c: object, guild_id: str, user_id: str) -> tuple[None, object]:
        return None, _CALLER

    monkeypatch.setattr(_routines_mod, "_resolve_member", fake_resolve_member)


# The caller as a guild member; channel permissions come from the fakes below.
_CALLER = MagicMock(guild_permissions=MagicMock(administrator=False))


def _perms(*, view: bool = True, send: bool = True, manage_threads: bool = False) -> object:
    return MagicMock(
        view_channel=view,
        send_messages=send,
        send_messages_in_threads=send,
        manage_threads=manage_threads,
    )


def _text_channel(
    *, guild_id: str = _GUILD, category_id: int | None = None, perms: object | None = None
) -> object:
    from types import SimpleNamespace

    import discord

    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=int(guild_id))
    channel.category_id = category_id
    channel.permissions_for = MagicMock(return_value=perms if perms is not None else _perms())
    return channel


def _private_thread(*, parent: object, caller_is_member: bool) -> object:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord

    thread = MagicMock(spec=discord.Thread)
    thread.guild = SimpleNamespace(id=int(_GUILD))
    thread.parent = parent
    thread.parent_id = 444
    thread.type = discord.ChannelType.private_thread
    thread.permissions_for = MagicMock(return_value=_perms())
    if caller_is_member:
        thread.fetch_member = AsyncMock(return_value=object())
    else:
        thread.fetch_member = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404, reason="Not Found"), "no")
        )
    return thread


def _slack_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_member: bool = True,
    info_error: str | None = None,
    thread_found: bool = True,
    is_private: bool = False,
    caller_in_channel: bool = True,
) -> None:
    from unittest.mock import AsyncMock

    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    client = MagicMock()
    if info_error is not None:
        response = MagicMock(spec=AsyncSlackResponse)
        response.data = {"ok": False, "error": info_error}
        client.conversations_info = AsyncMock(side_effect=SlackApiError("err", response))
    else:
        client.conversations_info = AsyncMock(
            return_value={
                "channel": {"id": "C0123ABC", "is_member": is_member, "is_private": is_private}
            }
        )
    client.users_info = AsyncMock(return_value={"user": {"id": "u_test"}})
    client.conversations_members = AsyncMock(
        return_value={"members": ["u_test"] if caller_in_channel else ["U_OTHER"]}
    )
    replies: dict[str, object] = {"messages": [{"ts": "1717.5"}] if thread_found else []}
    client.conversations_replies = AsyncMock(return_value=replies)

    async def fake_client(runtime: object, *, team_id: str) -> object:
        assert team_id == "T_TEST", "resolved in the caller's own workspace"
        return client

    monkeypatch.setattr(_routines_mod, "slack_web_client", fake_client)


async def _create(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    *,
    platform: str,
    kind: str,
    destination_id: str,
    policy: object | None = None,
) -> object:
    from daimon.core.stores.access_policy import set_access_policy

    tenant = await make_tenant(db_session, platform=platform)  # type: ignore[arg-type]
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)  # type: ignore[arg-type]
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    return await _create_routine_impl(
        _runtime(committing_sessionmaker, client=client),
        _auth_identity(
            tenant_id=tenant.id,
            platform=platform,
            external_id=_GUILD if platform == "discord" else "T_TEST",
            platform_user_id="111" if platform == "discord" else "u_test",
        ),
        agent_name="daimon",
        cron_expr="0 9 * * 1",
        timezone="UTC",
        trigger_message="weekly summary",
        destination_kind=kind,  # type: ignore[arg-type]
        destination_id=destination_id,
    )


async def test_create_routine_saves_a_reachable_destination_in_this_guild(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _discord_channels(monkeypatch, {1234: _text_channel()})
    row = await _create(
        committing_sessionmaker,
        db_session,
        platform="discord",
        kind="channel",
        destination_id="1234",
    )
    assert (row.destination_kind, row.destination_id) == ("channel", "1234")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("channels", "destination_id", "kind", "message"),
    [
        ({1234: "other-guild"}, "1234", "channel", "not in this server"),
        ({}, "1234", "channel", "cannot see"),
        ({1234: "text"}, "general", "channel", "invalid destination_id"),
        ({1234: "text"}, "1234", "thread", "use destination_kind=channel"),
    ],
)
async def test_create_routine_refuses_an_unusable_discord_destination(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    channels: dict[int, str],
    destination_id: str,
    kind: str,
    message: str,
) -> None:
    built = {
        cid: _text_channel(guild_id="999" if what == "other-guild" else _GUILD)
        for cid, what in channels.items()
    }
    _discord_channels(monkeypatch, built)
    with pytest.raises(ToolError, match=message):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="discord",
            kind=kind,
            destination_id=destination_id,
        )


async def test_create_routine_refuses_a_channel_in_a_protected_category(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.access_policy import TenantAccessPolicy

    _discord_channels(monkeypatch, {1234: _text_channel(category_id=77)})
    with pytest.raises(ToolError, match="protected channel"):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="discord",
            kind="channel",
            destination_id="1234",
            policy=TenantAccessPolicy(protected_category_ids=("77",)),
        )


@pytest.mark.parametrize(
    ("kwargs", "kind", "destination_id", "message"),
    [
        ({}, "thread", "C0123ABC", "invalid destination_id"),
        ({"is_member": False}, "channel", "C0123ABC", "invite it"),
        ({"info_error": "channel_not_found"}, "channel", "C0123ABC", "could not find"),
        ({"thread_found": False}, "thread", "C0123ABC:1717.5", "no thread"),
    ],
)
async def test_create_routine_refuses_an_unusable_slack_destination(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    kind: str,
    destination_id: str,
    message: str,
) -> None:
    _slack_client(monkeypatch, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ToolError, match=message):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="slack",
            kind=kind,
            destination_id=destination_id,
        )


async def test_create_routine_saves_a_slack_thread_destination(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _slack_client(monkeypatch)
    row = await _create(
        committing_sessionmaker,
        db_session,
        platform="slack",
        kind="thread",
        destination_id="C0123ABC:1717.5",
    )
    assert row.destination_id == "C0123ABC:1717.5"  # type: ignore[attr-defined]


async def test_create_routine_needs_both_destination_fields(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(ToolError, match="together"):
        await _create_routine_impl(
            _runtime(committing_sessionmaker),
            _auth_identity(tenant_id=tenant.id),
            agent_name="daimon",
            cron_expr="0 9 * * 1",
            timezone="UTC",
            trigger_message="x",
            destination_kind="channel",
        )


async def test_update_routine_sets_and_clears_a_destination(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _discord_channels(monkeypatch, {55: _text_channel()})
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u_test",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="orig",
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)
    auth = _auth_identity(tenant_id=tenant.id, external_id=_GUILD)

    set_ = await _update_routine_impl(
        runtime, auth, routine_id=created.id, destination_kind="channel", destination_id="55"
    )
    cleared = await _update_routine_impl(
        runtime, auth, routine_id=created.id, clear_destination=True
    )

    assert (set_.destination_kind, set_.destination_id) == ("channel", "55")
    assert set_.trigger_message == "orig"
    assert (cleared.destination_kind, cleared.destination_id) == (None, None)


@pytest.mark.parametrize(
    ("channel", "message"),
    [
        (lambda: _text_channel(perms=_perms(send=False)), "you cannot post"),
        (lambda: _text_channel(perms=_perms(view=False)), "you cannot post"),
    ],
)
async def test_create_routine_refuses_a_discord_channel_the_caller_cannot_post_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    channel: Any,
    message: str,
) -> None:
    """Review regression (round 3): the bot could post there, the caller could
    not — a routine must not become a way around send_message's checks."""
    _discord_channels(monkeypatch, {1234: channel()})
    with pytest.raises(ToolError, match=message):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="discord",
            kind="channel",
            destination_id="1234",
        )


async def test_create_routine_refuses_a_private_thread_the_caller_is_not_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _text_channel()
    _discord_channels(
        monkeypatch, {1234: _private_thread(parent=parent, caller_is_member=False), 444: parent}
    )
    with pytest.raises(ToolError, match="you cannot post"):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="discord",
            kind="thread",
            destination_id="1234",
        )


async def test_create_routine_accepts_a_private_thread_the_caller_is_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _text_channel()
    _discord_channels(
        monkeypatch, {1234: _private_thread(parent=parent, caller_is_member=True), 444: parent}
    )
    row = await _create(
        committing_sessionmaker,
        db_session,
        platform="discord",
        kind="thread",
        destination_id="1234",
    )
    assert row.destination_kind == "thread"  # type: ignore[attr-defined]


async def test_create_routine_refuses_a_private_slack_channel_the_caller_is_not_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review regression (round 3): daimon is in the private channel, the
    caller is not."""
    _slack_client(monkeypatch, is_private=True, caller_in_channel=False)
    with pytest.raises(ToolError, match="you cannot post"):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="slack",
            kind="channel",
            destination_id="C0123ABC",
        )
