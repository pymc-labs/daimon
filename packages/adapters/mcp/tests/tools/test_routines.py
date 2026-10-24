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

import asyncio
import dataclasses
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import daimon.adapters.mcp.tools.routines as _routines_mod
import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_panel_mcp_token
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role, RoutineRow
from daimon.core.stores.routines import create_routine, get_routine, list_routines_for_tenant
from daimon.core.stores.turn_origins import create_origin
from daimon.core.teams_graph import GraphUnavailable
from daimon.testing.asgi import INIT_BODY, INIT_HEADERS, asgi_lifespan, call_mcp_tool
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from mux.testing.drivers.anthropic.ma_models import ma_model_config
from pydantic import HttpUrl, PostgresDsn, SecretStr
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
    require_destination: bool = False,
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=client if client is not None else MagicMock(),  # type: ignore[arg-type]
        settings=MagicMock(  # type: ignore[arg-type]
            mcp=McpSettings(require_routine_destination=require_destination)
        ),
        deployment_default=DeploymentDefault(),
    )


def _auth_identity(
    *,
    platform: str | None = "discord",
    external_id: str | None = "g_test",
    platform_user_id: str | None = "u_test",
    tenant_id: uuid.UUID | None = None,
    is_admin: bool = False,
    talking_to: str | None = "ag_resolved",
) -> AuthIdentity:
    """A member in a chat turn with the MA agent ``talking_to`` (None: no agent)."""
    tenant_id = tenant_id if tenant_id is not None else uuid.uuid4()
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        platform=platform,
        external_id=external_id,
        platform_user_id=platform_user_id,
        is_admin=is_admin,
        chat_agent_id=(
            None
            if talking_to is None
            else derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=talking_to)
        ),
        chat_session_id="test-session",
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
    auth = _auth_identity(tenant_id=tenant_a.id, is_admin=True)
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
    auth = _auth_identity(tenant_id=tenant.id, is_admin=True)
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
    auth = _auth_identity(tenant_id=tenant.id, talking_to="agent_a")
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
    auth = _auth_identity(tenant_id=tenant.id, talking_to="agent_a")
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
    auth = _auth_identity(tenant_id=tenant.id, talking_to="agent_a")
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
    owner_auth = _auth_identity(tenant_id=tenant_owner.id, is_admin=True)
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
    auth = _auth_identity(tenant_id=tenant.id, talking_to="ag_other")
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
    auth = _auth_identity(
        tenant_id=tenant.id, platform_user_id="U_owner", is_admin=False, talking_to="agent_a"
    )
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
    owner = _auth_identity(tenant_id=tenant.id, platform_user_id="owner", talking_to="agent_policy")
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
    require_destination: bool = False,
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
        _runtime(committing_sessionmaker, client=client, require_destination=require_destination),
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


@pytest.mark.parametrize("require_destination", [False, True])
async def test_create_routine_saves_a_reachable_destination_in_this_guild(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    require_destination: bool,
) -> None:
    _discord_channels(monkeypatch, {1234: _text_channel()})
    row = await _create(
        committing_sessionmaker,
        db_session,
        platform="discord",
        kind="channel",
        destination_id="1234",
        require_destination=require_destination,
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
@pytest.mark.parametrize("require_destination", [False, True])
async def test_create_routine_refuses_an_unusable_discord_destination(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    channels: dict[int, str],
    destination_id: str,
    kind: str,
    message: str,
    require_destination: bool,
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
            require_destination=require_destination,
        )


@pytest.mark.parametrize("require_destination", [False, True])
async def test_create_routine_refuses_a_channel_in_a_protected_category(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    require_destination: bool,
) -> None:
    from daimon.core.access_policy import TenantAccessPolicy

    _discord_channels(monkeypatch, {1234: _text_channel(category_id=77)})
    with pytest.raises(ToolError, match="lets nobody write there"):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="discord",
            kind="channel",
            destination_id="1234",
            policy=TenantAccessPolicy(protected_category_ids=("77",)),
            require_destination=require_destination,
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
    assert row.channel_id == "C0123ABC", "spend is budgeted against the thread's channel"  # type: ignore[attr-defined]


def _teams_channels(
    monkeypatch: pytest.MonkeyPatch, *, member: bool, root: bool = True
) -> list[str]:
    located: list[str] = []
    client = MagicMock()
    client.is_member = AsyncMock(return_value=member)
    graph = MagicMock()
    graph.get_message = AsyncMock(side_effect=None if root else GraphUnavailable("x", status=404))
    monkeypatch.setattr(_routines_mod, "graph_for", lambda _client: graph)

    async def locate(runtime: object, auth: object, _client: object, channel_id: str) -> object:
        located.append(channel_id)
        return SimpleNamespace(channel_id=channel_id, channel_name="research", group_id="g")

    monkeypatch.setattr(_routines_mod, "require_client", lambda runtime, auth: (client, "u_test"))
    monkeypatch.setattr(_routines_mod, "locate_channel", locate)
    return located


@pytest.mark.parametrize("member", [True, False])
async def test_create_routine_saves_a_teams_thread_in_a_channel_the_caller_is_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    member: bool,
) -> None:
    located = _teams_channels(monkeypatch, member=member)
    thread = "19:chan@thread.tacv2;messageid=17"
    create = _create(
        committing_sessionmaker, db_session, platform="teams", kind="thread", destination_id=thread
    )
    if not member:
        with pytest.raises(ToolError, match="you are not in research"):
            await create
        return
    row = await create
    assert row.destination_id == thread  # type: ignore[attr-defined]
    assert row.channel_id == "19:chan@thread.tacv2", "budgeted against the thread's channel"  # type: ignore[attr-defined]
    assert located == ["19:chan@thread.tacv2"]


async def test_create_routine_refuses_a_teams_thread_that_does_not_exist(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _teams_channels(monkeypatch, member=True, root=False)
    with pytest.raises(ToolError, match="no thread 17 in research"):
        await _create(
            committing_sessionmaker,
            db_session,
            platform="teams",
            kind="thread",
            destination_id="19:chan@thread.tacv2;messageid=17",
        )


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


@pytest.mark.parametrize("require_destination", [False, True])
async def test_update_routine_sets_and_clears_a_destination(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    require_destination: bool,
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
    runtime = _runtime(committing_sessionmaker, require_destination=require_destination)
    auth = _auth_identity(tenant_id=tenant.id, external_id=_GUILD, talking_to="agent_a")

    set_ = await _update_routine_impl(
        runtime, auth, routine_id=created.id, destination_kind="channel", destination_id="55"
    )
    cleared = await _update_routine_impl(
        runtime, auth, routine_id=created.id, clear_destination=True
    )

    assert (set_.destination_kind, set_.destination_id, set_.channel_id) == ("channel", "55", "55")
    assert set_.trigger_message == "orig"
    assert (cleared.destination_kind, cleared.destination_id) == (None, None), "destination gone"
    assert cleared.channel_id == "55", "clearing the destination keeps the budget channel"


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
    assert row.channel_id == "444", "spend is budgeted against the thread's parent"  # type: ignore[attr-defined]


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


async def test_create_routine_without_a_destination_budgets_the_turns_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="444",
        thread_id="999",
        responder_ma_agent_id="ag_resolved",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + timedelta(minutes=10),
        now=now,
        chat_session_id="test-session",
    )
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker,
        client=_ma_client_with_agents(
            [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
        ),
    )
    auth = dataclasses.replace(_auth_identity(tenant_id=tenant.id), account_id=account.id)

    async def create(origin_context_id: str) -> RoutineRow:
        return await _create_routine_impl(
            runtime,
            auth,
            agent_name="daimon",
            cron_expr="0 9 * * 1",
            timezone="UTC",
            trigger_message="weekly summary",
            origin_context_id=origin_context_id,
        )

    here = await create(str(origin.id))
    assert here.channel_id == "444", "spend is budgeted against the channel it was made in"
    with pytest.raises(ToolError, match="source origin could not be verified"):
        await create(str(uuid.uuid4()))


async def test_create_routine_by_a_channel_bound_key_records_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """A key minted in a channel spends there, so its routine with no destination does too."""
    tenant = await make_tenant(db_session, platform="discord")
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker,
        client=_ma_client_with_agents(
            [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
        ),
    )
    key = dataclasses.replace(
        _auth_identity(tenant_id=tenant.id, talking_to=None),
        account_id=account.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_resolved"),
    )

    async def create(auth: AuthIdentity) -> RoutineRow:
        return await _create_routine_impl(
            runtime,
            auth,
            agent_name="daimon",
            cron_expr="0 9 * * 1",
            timezone="UTC",
            trigger_message="weekly summary",
        )

    from daimon.core.discord_membership import verify_discord_source

    async def verify(token, **kwargs):
        def channel(request):
            return httpx.Response(
                200, json={"id": "555", "guild_id": kwargs["guild_id"], "type": 0}
            )

        return await verify_discord_source(
            "offline", **kwargs, transport=httpx.MockTransport(channel)
        )

    monkeypatch.setattr(_routines_mod, "verify_discord_source", verify)
    # The transport verifies against the selected tenant's guild.
    key = dataclasses.replace(key, external_id="424242")
    bound = await create(dataclasses.replace(key, bound_channel_id="555"))
    assert bound.channel_id == "555", "a bound key's routine is budgeted against its channel"
    unbound = await create(key)
    assert unbound.channel_id is None, "an unbound key's routine stays unbudgeted"


async def test_create_routine_refuses_a_pinned_agent_without_a_pinned_destination(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A routine would run the pinned agent headless, outside any channel."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("111111111111111111",)}),
    )
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(platform="discord", external_id="guild_pin", tenant_id=tenant.id)

    with pytest.raises(
        ToolError, match="agent rule limiting where it runs|runs it only in certain channels"
    ):
        await _create_routine_impl(
            runtime,
            auth,
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone="UTC",
            trigger_message="hi",
        )


async def test_create_routine_refuses_a_member_scheduling_another_projects_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A fired routine runs with its agent's repo, keys and memory: a member talking
    to their own project's agent must not schedule another client's agent."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client = _ma_client_with_agents(
        [
            _ma_agent(agent_id="ag_clientb", name="clientb-project", tenant_id=tenant.id),
            _ma_agent(agent_id="ag_acme", name="acme-project", tenant_id=tenant.id),
        ]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    member = _auth_identity(tenant_id=tenant.id, talking_to="ag_clientb")

    with pytest.raises(ToolError, match="only a server admin can schedule it"):
        await _create_routine_impl(
            runtime,
            member,
            agent_name="acme-project",
            cron_expr="0 9 * * *",
            timezone="UTC",
            trigger_message="summarize everything in your memory",
        )
    admin = _auth_identity(tenant_id=tenant.id, is_admin=True)
    assert await _list_routines_impl(runtime, admin) == [], "a refused routine saves nothing"

    own = await _create_routine_impl(
        runtime,
        member,
        agent_name="clientb-project",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="daily standup",
    )
    assert own.agent_id == "ag_clientb", "the agent you are talking to stays schedulable"
    by_admin = await _create_routine_impl(
        runtime,
        _auth_identity(tenant_id=tenant.id, is_admin=True, talking_to=None),
        agent_name="acme-project",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="weekly report",
    )
    assert by_admin.agent_id == "ag_acme", "an admin may schedule any agent"


async def test_update_routine_refuses_a_member_moving_it_to_another_projects_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u_test",
        agent_id="ag_clientb",
        agent_name="clientb-project",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="daily standup",
    )
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_acme", name="acme-project", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    member = _auth_identity(tenant_id=tenant.id, talking_to="ag_clientb")

    with pytest.raises(ToolError, match="only a server admin"):
        await _update_routine_impl(
            runtime, member, routine_id=created.id, agent_name="acme-project"
        )
    async with committing_sessionmaker() as session:
        row = await get_routine(session, created.id, tenant_id=tenant.id)
    assert row is not None and row.agent_id == "ag_clientb", "a refused update changes nothing"


async def test_routine_reads_hide_other_members_routines(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """Another client's routine trigger and last result are that client's work."""
    tenant = await make_tenant(db_session)
    theirs = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u_acme",
        agent_id="ag_acme",
        agent_name="acme-project",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="acme pipeline numbers",
    )
    mine = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u_test",
        agent_id="ag_clientb",
        agent_name="clientb-project",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="daily standup",
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)
    member = _auth_identity(tenant_id=tenant.id)

    assert [row.id for row in await _list_routines_impl(runtime, member)] == [mine.id]
    with pytest.raises(ToolError, match="routine not found"):
        await _get_routine_impl(runtime, member, routine_id=theirs.id)
    admin = _auth_identity(tenant_id=tenant.id, is_admin=True)
    assert {row.id for row in await _list_routines_impl(runtime, admin)} == {theirs.id, mine.id}


_PINNED_CHANNEL = "111111111111111111"


@pytest.mark.parametrize("change", ["clear-destination", "switch-to-pinned-agent"])
async def test_update_routine_refuses_moving_a_pinned_agent_out_of_its_channels(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    change: str,
) -> None:
    """An update is a save too: it must not leave a pinned agent's routine posting
    anywhere but a pinned channel, whether by dropping the destination or by
    switching an unpinned routine onto the pinned agent. Admins included."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"daimon": (_PINNED_CHANNEL,)}),
    )
    pinned_routine = change == "clear-destination"
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u_test",
        agent_id="ag_resolved" if pinned_routine else "ag_other",
        agent_name="daimon" if pinned_routine else "other",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="daily",
        destination_kind="channel" if pinned_routine else None,
        destination_id=_PINNED_CHANNEL if pinned_routine else None,
    )
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    admin = _auth_identity(tenant_id=tenant.id, is_admin=True)

    with pytest.raises(
        ToolError, match="agent rule limiting where it runs|runs it only in certain channels"
    ):
        if pinned_routine:
            await _update_routine_impl(
                runtime, admin, routine_id=created.id, clear_destination=True
            )
        else:
            await _update_routine_impl(runtime, admin, routine_id=created.id, agent_name="daimon")
    async with committing_sessionmaker() as session:
        row = await get_routine(session, created.id, tenant_id=tenant.id)
    assert row is not None and row.agent_id == created.agent_id
    assert row.destination_id == created.destination_id, "a refused update changes nothing"


def _display_named_agent(tenant_id: uuid.UUID) -> dict[str, object]:
    return ma_agent(
        id="ag_resolved",
        name="Acme Display",
        model=ma_model_config("claude-sonnet-4-6", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: "acme-config"},
    ).model_dump(mode="json")


async def test_create_routine_checks_the_pin_on_every_name_of_the_resolved_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """The caller names the config name; the pin is on the display name."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"Acme Display": (_PINNED_CHANNEL,)}),
    )
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker, client=_ma_client_with_agents([_display_named_agent(tenant.id)])
    )

    with pytest.raises(
        ToolError, match="agent rule limiting where it runs|runs it only in certain channels"
    ):
        await _create_routine_impl(
            runtime,
            _auth_identity(tenant_id=tenant.id, is_admin=True),
            agent_name="acme-config",
            cron_expr="0 9 * * *",
            timezone="UTC",
            trigger_message="report",
        )


async def test_update_routine_checks_the_pin_on_the_existing_agents_display_name(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A routine saved before the pin, under the config name, must not keep being
    editable into running the pinned agent outside its channels."""
    tenant = await make_tenant(db_session)
    created = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u_test",
        agent_id="ag_resolved",
        agent_name="acme-config",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="report",
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"Acme Display": (_PINNED_CHANNEL,)}),
    )
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker, client=_ma_client_with_agents([_display_named_agent(tenant.id)])
    )

    with pytest.raises(
        ToolError, match="agent rule limiting where it runs|runs it only in certain channels"
    ):
        await _update_routine_impl(
            runtime,
            _auth_identity(tenant_id=tenant.id, is_admin=True),
            routine_id=created.id,
            trigger_message="report, but more",
        )


def _empty_display_agent(tenant_id: uuid.UUID) -> dict[str, object]:
    return ma_agent(
        id="ag_resolved",
        name="",
        model=ma_model_config("claude-sonnet-4-6", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: "acme-config"},
    ).model_dump(mode="json")


async def test_an_empty_name_pin_does_not_pin_a_routine_on_create_or_update(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A pin keyed by the empty name never makes a routine pinned (save-time rule).

    An agent whose display name is empty and whose config name is not pinned
    saves a routine with no destination, on create and on update.
    """
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"": (_PINNED_CHANNEL,)}),
    )
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker, client=_ma_client_with_agents([_empty_display_agent(tenant.id)])
    )
    auth = _auth_identity(tenant_id=tenant.id, is_admin=False)

    created = await _create_routine_impl(
        runtime,
        auth,
        agent_name="acme-config",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="report",
    )
    updated = await _update_routine_impl(
        runtime, auth, routine_id=created.id, trigger_message="report, but more"
    )
    assert updated.trigger_message == "report, but more"


_ROUTINE_DESTINATION_WARNING = (
    "No destination was supplied. This routine won't post, so nothing was created. "
    "Where should results be delivered?"
)


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_required_destination_refuses_before_database_and_agent_lookup(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    factory = MagicMock(side_effect=AssertionError("must not open a database session"))
    lookup = AsyncMock(side_effect=AssertionError("must not look up an agent"))
    monkeypatch.setattr(_routines_mod, "find_agent_by_daimon_tag", lookup)
    runtime = _runtime(factory, require_destination=True)

    with pytest.raises(ToolError) as refused:
        await _create_routine_impl(
            runtime,
            _auth_identity(platform=platform),
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone="UTC",
            trigger_message="ROUTINE-PING",
        )

    assert str(refused.value) == _ROUTINE_DESTINATION_WARNING
    factory.assert_not_called()
    lookup.assert_not_awaited()


@pytest.mark.parametrize("require_destination", [False, True])
@pytest.mark.parametrize(("kind", "destination_id"), [("channel", None), (None, "1234")])
async def test_destination_pair_validation_precedes_opt_in_warning(
    require_destination: bool, kind: str | None, destination_id: str | None
) -> None:
    factory = MagicMock(side_effect=AssertionError("must not open a database session"))
    with pytest.raises(
        ToolError, match="^destination_kind and destination_id must be given together$"
    ):
        await _create_routine_impl(
            _runtime(factory, require_destination=require_destination),
            _auth_identity(),
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone="UTC",
            trigger_message="ping",
            destination_kind=kind,  # type: ignore[arg-type]
            destination_id=destination_id,
        )
    factory.assert_not_called()


async def test_required_destination_preserves_platform_identity_refusal() -> None:
    factory = MagicMock(side_effect=AssertionError("must not open a database session"))
    with pytest.raises(ToolError, match="^creating a routine requires a platform user identity$"):
        await _create_routine_impl(
            _runtime(factory, require_destination=True),
            _auth_identity(platform_user_id=None),
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone="UTC",
            trigger_message="ping",
        )
    factory.assert_not_called()


@pytest.mark.parametrize("valid_token", [False, True])
async def test_required_destination_on_authenticated_public_mcp_surface(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    valid_token: bool,
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="requester", tenant=tenant, account=account
    )
    await db_session.commit()
    lookup = AsyncMock(side_effect=AssertionError("must not look up an agent"))
    monkeypatch.setattr(_routines_mod, "find_agent_by_daimon_tag", lookup)
    requests: list[httpx.Request] = []

    def unexpected_native(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("must not make a native request")

    secret = b"synthetic-routine-test-signing-key"
    async with build_fake_anthropic(unexpected_native) as client:
        app = create_mcp_app(
            settings=Settings(
                database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://unused/db")),
                anthropic=AnthropicSettings(api_key=SecretStr("synthetic-test-key")),
                mcp=McpSettings(
                    jwt_secret=SecretStr(secret.decode()),
                    public_url=HttpUrl("https://mcp.test.invalid/mcp"),
                    require_routine_destination=True,
                ),
                _env_file=None,  # type: ignore[call-arg]
            ),
            sessionmaker=committing_sessionmaker,
            anthropic=client,
        )
        async with committing_sessionmaker.begin() as token_db:
            token = await mint_panel_mcp_token(
                token_db,
                account_id=account.id,
                secret=secret if valid_token else b"different-synthetic-signing-key",
                now=datetime.now(UTC),
            )
        if valid_token:
            response = await call_mcp_tool(
                app,
                token=token,
                name="create_routine",
                arguments={
                    "agent_name": "daimon",
                    "cron_expr": "* * * * *",
                    "timezone": "UTC",
                    "trigger_message": "ROUTINE-PING",
                },
            )
            result = response["result"]
            assert isinstance(result, dict)
            assert result["isError"] is True
            assert result["content"] == [{"type": "text", "text": _ROUTINE_DESTINATION_WARNING}]
            # This is a real authenticated tool error, not yet a visible model reply.
            assert re.search(
                r"(?i)won'?t (post|be delivered)|no destination|where should",
                _ROUTINE_DESTINATION_WARNING,
            )
        else:
            async with (
                asgi_lifespan(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    trust_env=False,
                ) as http,
            ):
                headers = {**INIT_HEADERS, "Authorization": f"Bearer {token}"}
                response = await http.post("/mcp", json=INIT_BODY, headers=headers)
                assert response.status_code == 401
                assert _ROUTINE_DESTINATION_WARNING not in response.text

    lookup.assert_not_awaited()
    assert requests == []
    async with committing_sessionmaker() as session:
        assert await list_routines_for_tenant(session, tenant_id=tenant.id) == []


async def test_owner_dm_resolves_recipient_without_channel_lookup(
    committing_sessionmaker, db_session, monkeypatch
):
    tenant = await make_tenant(db_session, platform="discord")
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id="123")
    channel_lookup = AsyncMock(side_effect=AssertionError("DM recipients are not channels"))
    monkeypatch.setattr(_routines_mod, "_resolve_discord_destination", channel_lookup)
    row = await _create_routine_impl(
        runtime,
        auth,
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="summary",
        destination_kind="dm",
    )
    assert row.destination_id == "123"
    assert row.channel_id is None
    fetched = await _get_routine_impl(runtime, auth, routine_id=row.id)
    assert fetched.destination_kind == "dm" and fetched.destination_id == "123"
    assert fetched.delivery_status is None
    with pytest.raises(ToolError, match="authenticated owner"):
        await _create_routine_impl(
            runtime,
            auth,
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone="UTC",
            trigger_message="summary",
            destination_kind="dm",
            destination_id="999",
        )
    from daimon.core.stores.routines import record_result

    async with committing_sessionmaker.begin() as session:
        await record_result(session, row.id, tail="personal memory", error=None, delivery="pending")
    admin = dataclasses.replace(auth, platform_user_id="999", is_admin=True)
    owner_view = await _get_routine_impl(runtime, auth, routine_id=row.id)
    admin_view = await _get_routine_impl(runtime, admin, routine_id=row.id)
    assert owner_view.last_result_tail == "personal memory"
    assert admin_view.last_result_tail is None and admin_view.delivery_payload is None
    with pytest.raises(ToolError, match="authenticated owner"):
        await _update_routine_impl(runtime, admin, routine_id=row.id, clear_destination=True)
    with pytest.raises(ToolError, match="authenticated owner"):
        await _update_routine_impl(runtime, admin, routine_id=row.id, destination_kind="dm")
    channel_lookup.assert_not_awaited()
    await client.close()


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_dm_writes_rejected_on_unsupported_platform(platform):
    auth = _auth_identity(platform=platform, platform_user_id="123")
    with pytest.raises(ToolError, match="not supported here"):
        await _routines_mod._check_destination(  # pyright: ignore[reportPrivateUsage]
            MagicMock(), auth, kind="dm", destination_id=None
        )


@pytest.mark.parametrize("restricted", [False, True])
async def test_mcp_thread_to_dm_checks_and_retains_original_thread(
    committing_sessionmaker, db_session, monkeypatch, restricted
):
    from daimon.core.access_policy import ChannelRule
    from daimon.core.discord_membership import verify_discord_source
    from daimon.core.errors import StoreError

    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag_resolved",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="summary",
        destination_kind="thread",
        destination_id="789",
        channel_id="456",
    )
    if restricted:
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(channel_rules={"789": ChannelRule(readers="inside")}),
        )
    await db_session.commit()
    requests = []

    def channel(request):
        requests.append(request.url.path)
        key = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            json={
                "id": key,
                "guild_id": "424242",
                "type": 11 if key == "789" else 0,
                "parent_id": "456",
            },
        )

    async def verify(token, **kwargs):
        return await verify_discord_source(
            "offline", **kwargs, transport=httpx.MockTransport(channel)
        )

    monkeypatch.setattr(_routines_mod, "verify_discord_source", verify)
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id="123", external_id="424242")
    try:
        if restricted:
            with pytest.raises(StoreError, match="restricted source"):
                await _update_routine_impl(runtime, auth, routine_id=row.id, destination_kind="dm")
        else:
            converted = await _update_routine_impl(
                runtime, auth, routine_id=row.id, destination_kind="dm"
            )
            assert (converted.channel_id, converted.source_thread_id) == ("456", "789")
            async with committing_sessionmaker.begin() as session:
                await set_access_policy(
                    session,
                    tenant_id=tenant.id,
                    policy=TenantAccessPolicy(channel_rules={"789": ChannelRule(readers="inside")}),
                )
            with pytest.raises(StoreError, match="restricted source"):
                await _update_routine_impl(
                    runtime, auth, routine_id=row.id, trigger_message="edited"
                )
        async with committing_sessionmaker() as session:
            saved = await get_routine(session, row.id, tenant_id=tenant.id)
        assert saved is not None
        assert saved.destination_kind == ("thread" if restricted else "dm")
        assert requests == [], "conversion checks immutable creation provenance without REST"
    finally:
        await client.close()


@pytest.mark.parametrize("restricted", [None, "456", "789"])
async def test_mcp_dm_creation_verifies_and_retains_thread_origin(
    committing_sessionmaker, db_session, monkeypatch, restricted
):
    from daimon.core.access_policy import ChannelRule
    from daimon.core.discord_membership import verify_discord_source

    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="456",
        thread_id="789",
        responder_ma_agent_id="ag_resolved",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        expires_at=now + timedelta(minutes=10),
        now=now,
        chat_session_id="test-session",
        sender_foreign=False,
        shared_channel=False,
    )
    if restricted:
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(channel_rules={restricted: ChannelRule(readers="inside")}),
        )
    await db_session.commit()
    requests = []

    def channel(request):
        key = request.url.path.rsplit("/", 1)[1]
        requests.append(key)
        return httpx.Response(
            200,
            json={
                "id": key,
                "guild_id": "424242",
                "type": 11 if key == "789" else 0,
                "parent_id": "456",
            },
        )

    async def verify(token, **kwargs):
        return await verify_discord_source(
            "offline", **kwargs, transport=httpx.MockTransport(channel)
        )

    monkeypatch.setattr(_routines_mod, "verify_discord_source", verify)
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = dataclasses.replace(
        _auth_identity(tenant_id=tenant.id, platform_user_id="123", external_id="424242"),
        account_id=account.id,
    )
    try:

        async def create():
            return await _create_routine_impl(
                runtime,
                auth,
                agent_name="daimon",
                cron_expr="0 9 * * *",
                timezone="UTC",
                trigger_message="summary",
                destination_kind="dm",
                origin_context_id=str(origin.id),
            )

        if restricted:
            with pytest.raises(ToolError, match="restricted source"):
                await create()
        else:
            row = await create()
            assert (row.channel_id, row.source_thread_id) == ("456", "789")
        async with committing_sessionmaker() as session:
            rows = await list_routines_for_tenant(session, tenant_id=tenant.id)
        assert len(rows) == (0 if restricted else 1)
        assert requests == ["789", "456"]
    finally:
        await client.close()


@pytest.mark.parametrize("clear_first", [False, True])
@pytest.mark.parametrize("sealed_before", [False, True])
async def test_authenticated_mcp_dm_conversion_enforces_thread_seal(
    committing_sessionmaker, db_session, monkeypatch, sealed_before, clear_first
):
    from daimon.core.access_policy import ChannelRule
    from daimon.core.config import DiscordSettings
    from daimon.core.discord_membership import verify_discord_source

    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    from daimon.core.stores.accounts import set_role

    account = await make_account(db_session, tenant=tenant)
    await set_role(db_session, account.id, Role.ADMIN)
    await make_platform_principal(
        db_session, platform="discord", external_id="123", tenant=tenant, account=account
    )
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag_resolved",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="summary",
        destination_kind="thread",
        destination_id="789",
        channel_id="456",
    )
    seal = TenantAccessPolicy(channel_rules={"789": ChannelRule(readers="inside")})
    if sealed_before:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=seal)
    await db_session.commit()

    def channel(request):
        key = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            json={
                "id": key,
                "guild_id": "424242",
                "type": 11 if key == "789" else 0,
                "parent_id": "456",
            },
        )

    async def verify(token, **kwargs):
        return await verify_discord_source(
            "offline", **kwargs, transport=httpx.MockTransport(channel)
        )

    monkeypatch.setattr(_routines_mod, "verify_discord_source", verify)
    secret = b"synthetic-routine-test-signing-key"
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://unused/db")),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            discord=DiscordSettings(bot_token=SecretStr("offline")),
            mcp=McpSettings(
                jwt_secret=SecretStr(secret.decode()),
                public_url=HttpUrl("https://mcp.test.invalid/mcp"),
            ),
            _env_file=None,
        ),  # type: ignore[call-arg]
        sessionmaker=committing_sessionmaker,
        anthropic=client,
    )
    async with committing_sessionmaker.begin() as session:
        token = await mint_panel_mcp_token(
            session, account_id=account.id, secret=secret, now=datetime.now(UTC)
        )
    try:
        if clear_first:
            # Simulate an old row with no recorded parent or separate source fields.
            from sqlalchemy import text

            async with committing_sessionmaker.begin() as session:
                await session.execute(
                    text(
                        "UPDATE routines SET channel_id = NULL, source_channel_id = NULL, source_thread_id = NULL, source_provenance_known = false WHERE id = :id"
                    ),
                    {"id": row.id},
                )
            cleared = await call_mcp_tool(
                app,
                token=token,
                name="update_routine",
                arguments={"routine_id": str(row.id), "clear_destination": True},
            )
            assert not cleared["result"].get("isError")
            async with committing_sessionmaker.begin() as session:
                saved = await get_routine(session, row.id, tenant_id=tenant.id)
                assert saved is not None and saved.destination_kind is None
                assert (saved.source_channel_id, saved.source_thread_id) == (None, None)
                assert not saved.source_provenance_known
                await set_access_policy(session, tenant_id=tenant.id, policy=seal)
        response = await call_mcp_tool(
            app,
            token=token,
            name="update_routine",
            arguments={"routine_id": str(row.id), "destination_kind": "dm"},
        )
        assert bool(response["result"].get("isError")) == (sealed_before or clear_first)
        async with committing_sessionmaker() as session:
            saved = await get_routine(session, row.id, tenant_id=tenant.id)
        assert saved is not None
        assert saved.destination_kind == (
            None if clear_first else "thread" if sealed_before else "dm"
        )
        assert saved.delivery_payload is None
        if not sealed_before and not clear_first:
            assert (saved.channel_id, saved.source_thread_id) == ("456", "789")
            async with committing_sessionmaker.begin() as session:
                await set_access_policy(session, tenant_id=tenant.id, policy=seal)
            response = await call_mcp_tool(
                app,
                token=token,
                name="update_routine",
                arguments={"routine_id": str(row.id), "trigger_message": "admin edit"},
            )
            assert response["result"]["isError"] is True
            async with committing_sessionmaker() as session:
                saved = await get_routine(session, row.id, tenant_id=tenant.id)
            assert saved is not None and saved.trigger_message == "summary"
        from daimon.core.routine_delivery import poll_deliveries_once

        async def unexpected_dm_post(row):
            raise AssertionError("refused conversion must not cause DM I/O")

        assert (
            await poll_deliveries_once(
                committing_sessionmaker,
                platform="discord",
                post=unexpected_dm_post,
                now=datetime.now(UTC),
            )
            == 0
        )
    finally:
        await client.close()


@pytest.mark.parametrize("without_destination", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "channel",
        "thread",
        "cold",
        "unknown",
        "foreign",
        "restricted",
        "wrong_owner",
        "missing",
        "guild_thread",
    ],
)
async def test_authenticated_routine_creation_preserves_private_or_guild_origin(
    committing_sessionmaker, db_session, monkeypatch, source, without_destination
):
    from daimon.core.access_policy import ChannelRule
    from daimon.core.config import DiscordSettings
    from daimon.core.discord_membership import verify_discord_source
    from daimon.core.mcp_auth import mint_chat_mcp_token
    from daimon.core.stores.accounts import set_role
    from daimon.core.stores.direct_messages import (
        COLD_DM_ORIGIN,
        DirectMessageRow,
        start_conversation,
    )

    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    account = await make_account(db_session, tenant=tenant)
    await set_role(db_session, account.id, Role.ADMIN)
    await make_platform_principal(
        db_session, platform="discord", external_id="123", tenant=tenant, account=account
    )
    now = datetime.now(UTC)
    scope = f"dm:{uuid.uuid4()}"
    has_thread = source in ("thread", "restricted", "guild_thread")
    no_source = source in ("cold", "unknown", "missing")
    if source not in ("missing", "guild_thread"):
        owner = (
            await make_account(db_session, tenant=tenant) if source == "wrong_owner" else account
        )
        await start_conversation(
            db_session,
            now=now,
            conversation=DirectMessageRow(
                platform="discord",
                route_key="physical-dm",
                external_user_id="123",
                tenant_id=tenant.id,
                account_id=owner.id,
                workspace_id="424242",
                channel_id="99999",
                scope_id=scope,
                source_url=COLD_DM_ORIGIN if source == "cold" else "moved source",
                context="saved context",
                memory_read_only=True,
                source_channel_id=None if no_source else "456",
                source_thread_id="789" if has_thread else None,
                source_thread_keys=None,
                history=[],
                recent_message_ids=[],
                active_until=None,
            ),
        )
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="456" if source == "guild_thread" else "99999",
        thread_id="789" if source == "guild_thread" else scope,
        responder_ma_agent_id="ag_resolved",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.ADMIN,
        expires_at=now + timedelta(minutes=10),
        now=now,
        chat_session_id="test-session",
        sender_foreign=False,
        shared_channel=False,
    )
    if source == "restricted":
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(channel_rules={"789": ChannelRule(readers="inside")}),
        )
    await db_session.commit()
    lookups = []

    def channel(request):
        key = request.url.path.rsplit("/", 1)[1]
        lookups.append(key)
        assert key in ("456", "789"), "never verify a synthetic scope or physical DM"
        return httpx.Response(
            200,
            json={
                "id": key,
                "guild_id": "999" if source == "foreign" else "424242",
                "type": 11 if key == "789" else 0,
                "parent_id": "456",
            },
        )

    async def verify(token, **kwargs):
        return await verify_discord_source(
            "offline", **kwargs, transport=httpx.MockTransport(channel)
        )

    monkeypatch.setattr(_routines_mod, "verify_discord_source", verify)
    secret = b"synthetic-routine-test-signing-key"
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://unused/db")),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            discord=DiscordSettings(bot_token=SecretStr("offline")),
            mcp=McpSettings(
                jwt_secret=SecretStr(secret.decode()),
                public_url=HttpUrl("https://mcp.test.invalid/mcp"),
            ),
            _env_file=None,
        ),  # type: ignore[call-arg]
        sessionmaker=committing_sessionmaker,
        anthropic=client,
    )
    async with committing_sessionmaker.begin() as session:
        token, _, _ = await mint_chat_mcp_token(
            session,
            account_id=account.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_resolved"),
            secret=secret,
            chat_session_id="test-session",
            now=now,
        )
    arguments = {
        "agent_name": "daimon",
        "cron_expr": "0 9 * * *",
        "timezone": "UTC",
        "trigger_message": "summary",
        "origin_context_id": str(origin.id),
    }
    if not without_destination:
        arguments["destination_kind"] = "dm"
    refused = source in ("unknown", "foreign", "wrong_owner", "missing") or (
        source == "restricted" and not without_destination
    )
    try:
        response = await call_mcp_tool(app, token=token, name="create_routine", arguments=arguments)
        assert bool(response["result"].get("isError")) == refused
        async with committing_sessionmaker() as session:
            rows = await list_routines_for_tenant(session, tenant_id=tenant.id)
        assert len(rows) == (0 if refused else 1)
        if not refused:
            row = rows[0]
            assert (row.source_channel_id, row.source_thread_id) == (
                (None, None) if source == "cold" else ("456", "789" if has_thread else None)
            )
            if without_destination:
                # Sealing between creation and selection cannot erase the thread.
                if has_thread:
                    async with committing_sessionmaker.begin() as session:
                        await set_access_policy(
                            session,
                            tenant_id=tenant.id,
                            policy=TenantAccessPolicy(
                                channel_rules={"789": ChannelRule(readers="inside")}
                            ),
                        )
                converted = await call_mcp_tool(
                    app,
                    token=token,
                    name="update_routine",
                    arguments={"routine_id": str(row.id), "destination_kind": "dm"},
                )
                assert bool(converted["result"].get("isError")) == has_thread
                async with committing_sessionmaker() as session:
                    saved = await get_routine(session, row.id, tenant_id=tenant.id)
                assert saved is not None and saved.destination_kind == (
                    None if has_thread else "dm"
                )
                assert saved.delivery_payload is None
        if source in ("cold", "unknown", "wrong_owner", "missing"):
            assert lookups == []
        from daimon.core.routine_delivery import poll_deliveries_once

        async def unexpected_dm_post(row):
            raise AssertionError("routine configuration must not queue DM I/O")

        assert (
            await poll_deliveries_once(
                committing_sessionmaker, platform="discord", post=unexpected_dm_post, now=now
            )
            == 0
        )
    finally:
        await client.close()


@pytest.mark.parametrize(
    "platform,thread,parent",
    [
        ("slack", "C123:1710000000.000001", "C123"),
        ("teams", "19:channel@thread.tacv2;messageid=123", "19:channel@thread.tacv2"),
    ],
)
async def test_clearing_legacy_platform_thread_keeps_unknown_creation_snapshot(
    committing_sessionmaker, db_session, platform, thread, parent, monkeypatch
):
    tenant = await make_tenant(db_session, platform=platform)
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag_resolved",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="summary",
        destination_kind="thread",
        destination_id=thread,
    )
    await db_session.commit()
    monkeypatch.setattr(
        _routines_mod,
        "verify_discord_source",
        AsyncMock(side_effect=AssertionError("no Discord lookup for Slack/Teams")),
    )
    cleared = await _update_routine_impl(
        _runtime(committing_sessionmaker),
        _auth_identity(tenant_id=tenant.id, platform=platform, is_admin=True),
        routine_id=row.id,
        clear_destination=True,
    )
    assert cleared.destination_kind is None
    assert (cleared.source_channel_id, cleared.source_thread_id) == (None, thread)
    assert not cleared.source_provenance_known


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("target_kind", ["channel", "thread"])
async def test_authenticated_retarget_never_rewrites_creation_source(
    committing_sessionmaker, db_session, monkeypatch, known, target_kind
):
    from daimon.core.access_policy import ChannelRule
    from daimon.core.config import DiscordSettings
    from daimon.core.routine_delivery import poll_deliveries_once
    from daimon.core.stores.accounts import set_role

    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    account = await make_account(db_session, tenant=tenant)
    await set_role(db_session, account.id, Role.ADMIN)
    await make_platform_principal(
        db_session, platform="discord", external_id="123", tenant=tenant, account=account
    )
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag_resolved",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="summary",
        destination_kind="thread",
        destination_id="789",
        channel_id="456" if known else None,
        source_channel_id="456" if known else None,
        source_thread_id="789" if known else None,
        source_provenance_known=known,
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(channel_rules={"456": ChannelRule(readers="inside")}),
    )
    await db_session.commit()
    original = (row.source_channel_id, row.source_thread_id, row.source_provenance_known)
    resolutions = []

    async def destination(runtime, auth, *, kind, destination_id):
        resolutions.append(destination_id)
        assert destination_id in ("999", "888"), "never resolve an old source during an edit"
        return ("999" if kind == "thread" else None), None

    monkeypatch.setattr(_routines_mod, "_resolve_discord_destination", destination)
    monkeypatch.setattr(
        _routines_mod,
        "verify_discord_source",
        AsyncMock(side_effect=AssertionError("creation provenance is immutable")),
    )
    secret = b"synthetic-routine-test-signing-key"
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://unused/db")),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            discord=DiscordSettings(bot_token=SecretStr("offline")),
            mcp=McpSettings(
                jwt_secret=SecretStr(secret.decode()),
                public_url=HttpUrl("https://mcp.test.invalid/mcp"),
            ),
            _env_file=None,
        ),  # type: ignore[call-arg]
        sessionmaker=committing_sessionmaker,
        anthropic=client,
    )
    async with committing_sessionmaker.begin() as session:
        token = await mint_panel_mcp_token(
            session, account_id=account.id, secret=secret, now=datetime.now(UTC)
        )
    try:
        response = await call_mcp_tool(
            app,
            token=token,
            name="update_routine",
            arguments={
                "routine_id": str(row.id),
                "destination_kind": target_kind,
                "destination_id": "999" if target_kind == "channel" else "888",
            },
        )
        assert not response["result"].get("isError")
        async with committing_sessionmaker() as session:
            saved = await get_routine(session, row.id, tenant_id=tenant.id)
        assert saved is not None and saved.channel_id == "999"
        assert (
            saved.source_channel_id,
            saved.source_thread_id,
            saved.source_provenance_known,
        ) == original
        if target_kind == "thread":
            cleared = await call_mcp_tool(
                app,
                token=token,
                name="update_routine",
                arguments={"routine_id": str(row.id), "clear_destination": True},
            )
            assert not cleared["result"].get("isError")
        converted = await call_mcp_tool(
            app,
            token=token,
            name="update_routine",
            arguments={"routine_id": str(row.id), "destination_kind": "dm"},
        )
        assert converted["result"]["isError"] is True
        error = str(converted["result"]["content"])
        assert ("restricted source" if known else "Recreate the routine") in error
        async with committing_sessionmaker() as session:
            saved = await get_routine(session, row.id, tenant_id=tenant.id)
        assert saved is not None and saved.destination_kind == (
            "channel" if target_kind == "channel" else None
        )
        assert (
            saved.source_channel_id,
            saved.source_thread_id,
            saved.source_provenance_known,
        ) == original
        assert resolutions == ["999" if target_kind == "channel" else "888"]

        async def unexpected_post(row):
            raise AssertionError("refused conversion must perform zero DM I/O")

        assert (
            await poll_deliveries_once(
                committing_sessionmaker,
                platform="discord",
                post=unexpected_post,
                now=datetime.now(UTC),
            )
            == 0
        )
    finally:
        await client.close()


@pytest.mark.parametrize("change", ["agent", "retarget"])
async def test_concurrent_public_edit_cannot_expose_private_metadata(
    committing_sessionmaker, monkeypatch, change
):
    from daimon.adapters.mcp.tools._rule_view import CallerView
    from daimon.core.errors import StoreError
    from daimon.core.stores import routines
    from sqlalchemy.sql.dml import Update

    async with committing_sessionmaker.begin() as db:
        tenant = await make_tenant(db, platform="discord")
        public = await create_routine(
            db,
            tenant_id=tenant.id,
            created_by_user_id="123",
            agent_id="ag_a",
            agent_name="a",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="work",
            destination_kind="channel",
            destination_id="444",
            channel_id="444",
            source_provenance_known=True,
        )
    runtime = _runtime(committing_sessionmaker)
    admin = _auth_identity(tenant_id=tenant.id, platform_user_id="999", is_admin=True)
    owner = dataclasses.replace(admin, platform_user_id="123")
    only_b = CallerView(
        TenantAccessPolicy(), restrict_to_location=True, routed_agent_names=frozenset({"b"})
    )
    open_view = CallerView(TenantAccessPolicy())

    async def view(_runtime, auth):
        return only_b if auth.platform_user_id == "123" else open_view

    monkeypatch.setattr(_routines_mod, "load_isolation_view", view)
    read, release = asyncio.Event(), asyncio.Event()
    edits = (
        {"agent_id": "ag_b", "agent_name": "b"}
        if change == "agent"
        else {"destination_kind": "channel", "destination_id": "555", "channel_id": "555"}
    )

    async def stale_edit():
        async with committing_sessionmaker.begin() as db:
            execute = db.execute

            async def paused_execute(statement, *args, **kwargs):
                if isinstance(statement, Update) and statement.table.name == "routines":
                    read.set()
                    await release.wait()
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", paused_execute)
            await routines.update_routine(db, public.id, tenant_id=tenant.id, **edits)

    task = asyncio.create_task(stale_edit())
    await asyncio.wait_for(read.wait(), 15)
    async with committing_sessionmaker.begin() as db:
        private = await routines.update_routine(
            db, public.id, tenant_id=tenant.id, destination_kind="dm"
        )
        assert private is not None
        await routines.record_result(
            db,
            public.id,
            tail="private A text",
            error="private A diagnostic",
            delivery="pending",
            expected_private=private,
        )
    release.set()
    with pytest.raises(StoreError, match="changed while being edited"):
        await task
    hidden = await _get_routine_impl(runtime, admin, routine_id=public.id)
    assert hidden.last_result_tail is None and hidden.delivery_payload is None
    assert hidden.last_error == "Private routine run failed."
    with pytest.raises(ToolError, match="not found"):
        await _get_routine_impl(runtime, owner, routine_id=public.id)
    assert await _list_routines_impl(runtime, owner) == []

    async with committing_sessionmaker.begin() as db:
        await routines.update_routine(db, public.id, tenant_id=tenant.id, **edits)
    readers = (admin, owner) if change == "agent" else (admin,)
    for reader in readers:
        fetched = await _get_routine_impl(runtime, reader, routine_id=public.id)
        listed = await _list_routines_impl(runtime, reader)
        assert len(listed) == 1
        for row in (fetched, listed[0]):
            assert row.last_result_tail is None and row.last_error is None
            assert row.delivery_payload is None


async def test_update_rejects_a_revision_changed_after_mcp_authorization(
    committing_sessionmaker, monkeypatch
):
    from daimon.core.errors import StoreError
    from daimon.core.stores import routines

    async with committing_sessionmaker.begin() as db:
        tenant = await make_tenant(db, platform="discord")
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_resolved", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    auth = _auth_identity(tenant_id=tenant.id, platform_user_id="123")
    try:
        row = await _create_routine_impl(
            runtime,
            auth,
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone="UTC",
            trigger_message="work",
        )
        original_update = routines.update_routine

        async def intervening_edit(db, routine_id, **kwargs):
            async with committing_sessionmaker.begin() as other:
                await original_update(other, routine_id, tenant_id=tenant.id, destination_kind="dm")
            return await original_update(db, routine_id, **kwargs)

        monkeypatch.setattr(routines, "update_routine", intervening_edit)
        with pytest.raises(StoreError, match="changed while being edited"):
            await _update_routine_impl(runtime, auth, routine_id=row.id, trigger_message="stale")
        async with committing_sessionmaker() as db:
            current = await get_routine(db, row.id, tenant_id=tenant.id)
        assert current is not None and current.destination_kind == "dm"
        assert current.trigger_message == "work"
    finally:
        await client.close()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_legacy_private_output_on_all_mcp_reads(
    committing_sessionmaker, db_session, enabled, failed
):
    from sqlalchemy import text

    tenant = await make_tenant(db_session, platform="discord")
    await db_session.commit()
    client = _ma_client_with_agents(
        [_ma_agent(agent_id="ag_legacy", name="daimon", tenant_id=tenant.id)]
    )
    runtime = _runtime(committing_sessionmaker, client=client)
    async with committing_sessionmaker.begin() as session:
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id="123",
            agent_id="ag_legacy",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="legacy",
            enabled=enabled,
        )
        await session.execute(
            text(
                "UPDATE routines SET last_result_tail = 'personal-memory-sentinel', "
                "last_error = :error, delivery_payload = 'private-payload', "
                "delivery_note = 'private-diagnostic' WHERE id = :id"
            ),
            {"id": row.id, "error": "private-error" if failed else None},
        )
    owner = _auth_identity(tenant_id=tenant.id, platform_user_id="123", talking_to="ag_legacy")
    admin = dataclasses.replace(owner, platform_user_id="999", is_admin=True)
    for reader in (owner, admin, dataclasses.replace(admin, platform_user_id=None)):
        results = [
            await _get_routine_impl(runtime, reader, routine_id=row.id),
            *(await _list_routines_impl(runtime, reader)),
            await _update_routine_impl(runtime, reader, routine_id=row.id),
        ]
        assert len(results) == 3
        for result in results:
            if reader == owner:
                assert result.last_result_tail == "personal-memory-sentinel"
                assert result.delivery_payload == "private-payload"
                assert result.delivery_note == "private-diagnostic"
                assert result.last_error == ("private-error" if failed else None)
            else:
                assert result.last_result_tail is None
                assert result.delivery_payload is None and result.delivery_note is None
                assert "private-error" not in result.model_dump_json()
                assert "personal-memory-sentinel" not in result.model_dump_json()
    outsider = dataclasses.replace(admin, is_admin=False)
    assert await _list_routines_impl(runtime, outsider) == []
    with pytest.raises(ToolError, match="routine not found"):
        await _get_routine_impl(runtime, outsider, routine_id=row.id)
    await client.close()
