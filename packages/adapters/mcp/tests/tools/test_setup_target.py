"""Setup control authority and MA identity pinning through real stores and SDK transport."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.setup_target import (
    _set_setup_target_impl,
    require_turn_origin,
    resolve_setup_agent,
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_agent_bindings import create_binding, get_binding
from daimon.core.stores.turn_origins import create_origin, get_active_origin
from daimon.core.turn_origin import turn_origin
from daimon.testing import ma_agent, ma_model_config
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], client: AsyncAnthropic) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=client,
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
        ),
        deployment_default=DeploymentDefault(),
    )


async def test_switch_target_updates_shared_binding_and_only_requesting_snapshot(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    participant = await make_account(db_session, tenant=tenant)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="thread",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id="agent_original",
        configuration_target_name="original",
        creator_account_id=caller.id,
    )
    await db_session.commit()
    target = ma_agent(
        id="agent_new",
        name="new",
        model=ma_model_config("claude-sonnet-5", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant.id), MA_METADATA_KEY_NAME: "new"},
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([target.model_dump(mode="json")]))
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(router.dispatch))
    auth = AuthIdentity(
        account_id=caller.id, tenant_id=tenant.id, role=Role.USER, platform="discord"
    )
    async with (
        turn_origin(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=caller.id,
            platform="discord",
            parent_channel_id="parent",
            thread_id="thread",
            responder_ma_agent_id="agent_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id="agent_original",
            configuration_target_name="original",
            role=Role.USER,
            is_setup=True,
        ) as origin,
        turn_origin(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=participant.id,
            platform="discord",
            parent_channel_id="parent",
            thread_id="thread",
            responder_ma_agent_id="agent_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id="agent_original",
            configuration_target_name="original",
            role=Role.USER,
            is_setup=True,
        ) as concurrent,
    ):
        result = await _set_setup_target_impl(
            runtime, auth, origin_context_id=str(origin.id), agent_id="agent_new"
        )
        assert result.configuration_target_ma_agent_id == target.id, (
            "requesting turn sees selected id"
        )
        async with committing_sessionmaker() as session:
            binding = await get_binding(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="parent",
                thread_id="thread",
            )
            other = await get_active_origin(
                session,
                origin_id=concurrent.id,
                tenant_id=tenant.id,
                account_id=participant.id,
                platform="discord",
                now=datetime.now(UTC),
            )
        assert binding is not None and binding.configuration_target_ma_agent_id == target.id, (
            "future turns inherit the shared selection"
        )
        assert binding.responder_ma_agent_id == "agent_daimon", "selection must not change routing"
        assert other is not None and other.configuration_target_ma_agent_id == "agent_original", (
            "other running turns retain their own target snapshots"
        )


@pytest.mark.parametrize("mismatch", ["caller", "tenant", "platform", "expired", "invalid"])
async def test_tool_rejects_wrong_or_expired_origin_before_ma_access(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    mismatch: str,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        parent_channel_id="C123",
        thread_id="123.456",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        now=now,
        expires_at=now + timedelta(minutes=-1 if mismatch == "expired" else 10),
        is_setup=True,
    )
    await db_session.commit()
    auth = AuthIdentity(
        account_id=account.id, tenant_id=tenant.id, role=Role.USER, platform="slack"
    )
    if mismatch == "caller":
        auth = replace(auth, account_id=uuid.uuid4())
    elif mismatch == "tenant":
        auth = replace(auth, tenant_id=uuid.uuid4())
    elif mismatch == "platform":
        auth = replace(auth, platform="discord")
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(MARouter().dispatch))
    with pytest.raises(ToolError, match="origin"):
        await _set_setup_target_impl(
            runtime,
            auth,
            origin_context_id="invalid" if mismatch == "invalid" else str(origin.id),
            agent_id="agent_target",
        )


async def test_identity_pin_refuses_deleted_recreated_namesake(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    recreated = ma_agent(
        id="agent_recreated",
        name="specialist",
        model=ma_model_config("claude-sonnet-5", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: "specialist"},
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/agents", lambda _r, _m: list_response([recreated.model_dump(mode="json")])
    )
    runtime = _runtime(sessionmaker, build_fake_anthropic(router.dispatch))
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, platform="discord"
    )
    with pytest.raises(ToolError, match="missing or changed"):
        await resolve_setup_agent(
            runtime, auth, name="specialist", expected_ma_agent_id="agent_deleted"
        )
    with pytest.raises(ToolError, match="expected_ma_agent_id"):
        await resolve_setup_agent(runtime, auth, name="specialist")
    selected = await resolve_setup_agent(
        runtime, auth, name="specialist", expected_ma_agent_id="agent_recreated"
    )
    assert selected.id == recreated.id, (
        "explicitly choosing the current identity permits resolution"
    )


async def test_origin_refuses_agent_scoped_token_for_another_responder(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(MARouter().dispatch))
    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="thread",
        responder_ma_agent_id="agent_daimon",
        responder_name="Daimon",
        role=Role.USER,
    ) as origin:
        auth = AuthIdentity(
            account_id=account.id,
            tenant_id=tenant.id,
            role=Role.USER,
            platform="discord",
            agent_id=uuid.uuid4(),
        )
        with pytest.raises(ToolError, match="another responder"):
            await require_turn_origin(runtime, auth, str(origin.id))


@pytest.mark.parametrize("is_admin", [True, False])
async def test_model_change_uses_specialist_identity_and_retains_admin_gate(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    is_admin: bool,
) -> None:
    import re

    import httpx
    from daimon.adapters.mcp.tools.agents import _update_agent_impl
    from daimon.core.scope import ChannelScopeRef
    from daimon.core.stores.scoped_config_write import set_fields
    from daimon.testing.ma import json_body

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="parent"),
        tenant_id=tenant.id,
        agent_name="specialist",
        mode="agent",
        actor_account_id=account.id,
    )
    await db_session.commit()
    specialist = ma_agent(
        id="agent_specialist",
        name="specialist",
        model=ma_model_config("claude-sonnet-5", speed="standard"),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant.id),
            MA_METADATA_KEY_NAME: "specialist",
            "daimon_account": str(account.id),
        },
    )
    updates: list[str] = []

    def update_specialist(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        updates.append(request.url.path)
        assert json_body(request)["model"] == "claude-opus-5", "the requested model is applied"
        updated = ma_agent(
            id="agent_specialist",
            name="specialist",
            model=ma_model_config("claude-opus-5", speed="standard"),
            metadata={
                MA_METADATA_KEY_TENANT: str(tenant.id),
                MA_METADATA_KEY_NAME: "specialist",
                "daimon_account": str(account.id),
            },
            version=2,
            created_at=specialist.created_at,
        )
        return httpx.Response(200, json=updated.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET", r"/v1/agents", lambda _r, _m: list_response([specialist.model_dump(mode="json")])
    )
    router.add(
        "GET",
        r"/v1/agents/agent_specialist",
        lambda _r, _m: httpx.Response(200, json=specialist.model_dump(mode="json")),
    )
    router.add("POST", r"/v1/agents/agent_specialist", update_specialist)
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(router.dispatch))
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN if is_admin else Role.USER,
        is_admin=is_admin,
        platform="discord",
    )
    if is_admin:
        result = await _update_agent_impl(
            runtime,
            auth,
            "specialist",
            expected_ma_agent_id="agent_specialist",
            model="claude-opus-5",
            description=None,
            system=None,
            tools=None,
            mcp_servers=None,
            skills=None,
        )
        assert result.id == "agent_specialist", "setup changes the selected specialist"
        assert updates == ["/v1/agents/agent_specialist"], "Daimon is never mutated"
    else:
        with pytest.raises(ToolError, match="ask a workspace or server admin"):
            await _update_agent_impl(
                runtime,
                auth,
                "specialist",
                expected_ma_agent_id="agent_specialist",
                model="claude-opus-5",
                description=None,
                system=None,
                tools=None,
                mcp_servers=None,
                skills=None,
            )
        assert not updates, "setup entry does not grant members new editing permissions"


async def test_setup_target_refuses_in_a_handoff_thread_and_names_who_answers(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A handed-over thread has a responder but no configuration target; saying
    'this is not a setup conversation' would leave the caller guessing why."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="handed-over",
        responder_ma_agent_id="agent_research",
        responder_name="research-bot",
        kind="handoff",
    )
    await db_session.commit()
    target = ma_agent(
        id="agent_new",
        name="new",
        model=ma_model_config("claude-sonnet-5", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant.id), MA_METADATA_KEY_NAME: "new"},
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([target.model_dump(mode="json")]))
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(router.dispatch))
    auth = AuthIdentity(
        account_id=caller.id, tenant_id=tenant.id, role=Role.USER, platform="discord"
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="handed-over",
        responder_ma_agent_id="agent_research",
        responder_name="research-bot",
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError, match="handoff conversation: research-bot answers here"):
            await _set_setup_target_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id="agent_new"
            )

    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="parent",
            thread_id="handed-over",
        )
    assert binding is not None and binding.configuration_target_ma_agent_id is None, (
        "a handoff thread must not acquire a configuration target"
    )


async def test_setup_target_refusal_in_ordinary_chat_keeps_configuration_open(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An ordinary thread has no binding at all. The refusal must say only that
    there is no target to switch — a refusal that reads as 'this agent cannot be
    configured here' costs the caller the turn and the next one too."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    target = ma_agent(
        id="agent_new",
        name="new",
        model=ma_model_config("claude-sonnet-5", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant.id), MA_METADATA_KEY_NAME: "new"},
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([target.model_dump(mode="json")]))
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(router.dispatch))
    auth = AuthIdentity(
        account_id=caller.id, tenant_id=tenant.id, role=Role.USER, platform="discord"
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="parent",
        thread_id="ordinary",
        responder_ma_agent_id="agent_daimon",
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError) as refused:
            await _set_setup_target_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id="agent_new"
            )

    message = str(refused.value)
    assert "no selected target to switch" in message, (
        "the refusal must name what is actually missing: a selected target"
    )
    assert "update_agent" in message, (
        "the refusal must hand the caller a tool it can use in this same thread"
    )
    assert "does not block" in message, (
        "the refusal must say configuration is still available, or the model stops trying"
    )
    assert "/agent-setup" not in message, "the refusal must name a reachable control, not a path"
