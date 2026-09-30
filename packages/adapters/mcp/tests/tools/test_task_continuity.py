"""Handing a task over, and starting fresh, through real stores and SDK transport.

The load-bearing properties: a switch costs no billed turn and writes exactly
one thread-scoped row; every refusal writes nothing at all; and neither tool
ever touches channel or workspace routing.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.task_continuity import (
    _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
    _start_fresh_task_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.scope import DeploymentDefault
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.task_continuations import list_pending_continuations
from daimon.core.stores.thread_agent_bindings import create_binding, get_binding
from daimon.core.stores.thread_sessions import create_thread_session, get_live_thread_session
from daimon.core.stores.turn_origins import create_origin
from daimon.core.turn_origin import turn_origin
from daimon.testing import ma_agent, ma_model_config
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DESTINATION_ID = "agt_research"
_DESTINATION_NAME = "research-bot"
_RESPONDER_ID = "agt_daimon"


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic,
    *,
    default_agent_name: str | None = _DESTINATION_NAME,
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=client,
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
        ),
        # The deployment default is the cheapest way to make one agent
        # reachable: on a tenant with no config rows it is what every mention
        # resolves to.
        deployment_default=DeploymentDefault(agent_name=default_agent_name),
    )


def _destination(tenant_id: uuid.UUID, *, agent_id: str = _DESTINATION_ID) -> dict[str, object]:
    agent = ma_agent(
        id=agent_id,
        name=_DESTINATION_NAME,
        model=ma_model_config("claude-sonnet-5", speed="standard"),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: _DESTINATION_NAME,
        },
    )
    return agent.model_dump(mode="json")


def _client(agents: list[dict[str, object]]) -> AsyncAnthropic:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response(agents))
    return build_fake_anthropic(router.dispatch)


def _snapshot(*, repo_url: str | None) -> SessionSnapshot:
    return SessionSnapshot(
        ma_agent_id=_RESPONDER_ID,
        model_id="claude-sonnet-5",
        system_sha256=None,
        skills_sha256="skills",
        environment_id="env_1",
        repo_url=repo_url,
        repo_branch=None if repo_url is None else "main",
        memory_store_id=None,
        vault_id=None,
        tools_sha256="tools",
        mcp_servers_sha256="mcp",
        env_sha256=None,
        agent_version=1,
        agent_name="daimon",
    )


async def _count_routing_rows(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[int, int]:
    """How many channel-scope and workspace-scope routing rows this tenant has.

    Read through the store rather than the ORM: what matters is what the
    cascade would resolve, which is exactly what this read returns.
    """
    tenant_row, channel_rows = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    return len(channel_rows), 0 if tenant_row is None else 1


async def test_handoff_binds_the_thread_and_writes_no_channel_or_workspace_routing(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A switch alone: one thread row, no routing change, no billed turn."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _hand_off_task_impl(
            runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
        )

    assert result.destination_ma_agent_id == _DESTINATION_ID, "the concrete identity is bound"
    assert result.previous_responder_name == "daimon", "the result names who was answering"
    assert result.continuation_recorded is False, "a switch alone queues no work"
    assert (
        "research-bot takes over this task from your next message here." in result.confirmation
    ), "the tool returns final copy the model can relay without a model turn"
    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
        )
        channels, tenants = await _count_routing_rows(session, tenant.id)
        pending = await list_pending_continuations(
            session, tenant_id=tenant.id, platform="discord", thread_id="T_THREAD"
        )
    assert binding is not None and binding.kind == "handoff", "the thread records a handoff"
    assert binding.responder_ma_agent_id == _DESTINATION_ID, "the destination answers from now on"
    assert (channels, tenants) == (0, 0), (
        "parent-channel and workspace routing must be untouched by a handoff"
    )
    assert pending == [], "no continuation is queued when none was asked for"


async def test_handoff_with_a_continuation_queues_the_work_bounded_to_its_limit(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )
    long_request = "finish the churn writeup " * 40

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _hand_off_task_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            agent_id=_DESTINATION_ID,
            continuation=long_request,
        )

    assert result.continuation_recorded is True, "work was queued for the destination"
    async with committing_sessionmaker() as session:
        pending = await list_pending_continuations(
            session, tenant_id=tenant.id, platform="discord", thread_id="T_THREAD"
        )
    assert len(pending) == 1, "exactly one continuation is queued per handoff"
    queued = pending[0]
    assert queued.target_ma_agent_id == _DESTINATION_ID, (
        "the continuation is addressed to the concrete agent the caller chose"
    )
    assert queued.reason == "task_handoff", "the producer is recorded for the audit trail"
    assert queued.requested_work is not None and len(queued.requested_work) == 500, (
        "the person's words are bounded before they reach a turn"
    )
    assert queued.requested_work == long_request[:500], "and otherwise preserved verbatim"


@pytest.mark.parametrize(
    "continuation",
    ["", "   ", "ok", "short-1", _DESTINATION_NAME, f"  {_DESTINATION_NAME.upper()}  "],
    ids=["empty", "blank", "too_short", "seven_chars", "destination_name", "destination_name_ci"],
)
async def test_handoff_nulls_a_fabricated_or_switch_only_continuation(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    continuation: str,
) -> None:
    """Issue 2 (staging QA, 2026-09-13): a bare "take over" must never bill a
    second turn. The deterministic guard catches the shapes that are never a
    real work description regardless of wording -- empty, too short, or just
    the destination's own name -- and nulls them before a continuation is
    ever queued."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _hand_off_task_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            agent_id=_DESTINATION_ID,
            continuation=continuation,
        )

    assert result.continuation_recorded is False, (
        f"continuation={continuation!r} must be nulled, not queued as work"
    )
    assert "It will pick up with:" not in result.confirmation, (
        "a nulled continuation must not surface in the person-facing confirmation"
    )
    async with committing_sessionmaker() as session:
        pending = await list_pending_continuations(
            session, tenant_id=tenant.id, platform="discord", thread_id="T_THREAD"
        )
    assert pending == [], "nothing is queued when the continuation was nulled"


async def test_handoff_confirmation_renders_the_channel_as_a_mention(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Issue 3 (staging QA, 2026-09-13): the confirmation must never print a
    raw platform channel id."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _hand_off_task_impl(
            runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
        )

    assert "<#C_PARENT>" in result.confirmation, (
        f"expected a channel mention, got: {result.confirmation!r}"
    )
    assert "C_PARENT is unchanged" not in result.confirmation, (
        "the raw channel id must never appear unwrapped in person-facing copy"
    )


async def test_handoff_unreachable_refusal_renders_the_channel_as_a_mention(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker, _client([_destination(tenant.id)]), default_agent_name="daimon"
    )
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError, match=r"<#C_PARENT>") as excinfo:
            await _hand_off_task_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
            )
    assert "make research-bot answer in <#C_PARENT>" in str(excinfo.value)


async def test_handoff_refuses_an_agent_nobody_can_reach_and_writes_nothing(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker, _client([_destination(tenant.id)]), default_agent_name="daimon"
    )
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError, match="does not answer anywhere in this workspace"):
            await _hand_off_task_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
            )

    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
        )
    assert binding is None, "a refused handoff leaves the thread unbound"


async def test_handoff_refuses_inside_a_setup_conversation(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Setup threads answer as Daimon; admission would break if one were rebound."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="SETUP",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="SETUP",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
        is_setup=True,
    ) as origin:
        with pytest.raises(ToolError, match="Setup conversations always answer as Daimon"):
            await _hand_off_task_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
            )

    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="SETUP",
        )
    assert binding is not None and binding.responder_ma_agent_id == _RESPONDER_ID, (
        "the setup conversation keeps its Daimon responder"
    )
    assert binding.kind == "setup", "and stays a setup conversation"


async def test_handoff_refuses_a_recreated_namesakes_old_id(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Identity is the id: the name still resolves, the id the caller held does not."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(
        committing_sessionmaker, _client([_destination(tenant.id, agent_id="agt_research_v2")])
    )
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError, match="recreated and has a new id"):
            await _hand_off_task_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
            )

    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
        )
    assert binding is None, "a namesake must not silently receive the task"


async def test_handoff_refuses_the_agent_that_already_answers_here(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_DESTINATION_ID,
        responder_name=_DESTINATION_NAME,
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError, match="already answers in this conversation"):
            await _hand_off_task_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
            )


async def test_handoff_asks_once_about_uncommitted_work_then_proceeds_with_the_answer(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A bound repo is the meaningful-choice case: ask, write nothing, and let the
    answer ride the retry."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T_THREAD",
        account_id=caller.id,
        ma_session_id="sess_1",
        effective_config=_snapshot(repo_url="https://github.com/acme/data"),
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError, match="uncommitted changes in https://github.com/acme/data"):
            await _hand_off_task_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
            )
        async with committing_sessionmaker() as session:
            unwritten = await get_binding(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="C_PARENT",
                thread_id="T_THREAD",
            )
        assert unwritten is None, "asking the question must change nothing"

        result = await _hand_off_task_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            agent_id=_DESTINATION_ID,
            unsaved_work="leave",
        )

    assert result.destination_ma_agent_id == _DESTINATION_ID, "the answered retry goes through"

    async with committing_sessionmaker() as session:
        row = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T_THREAD",
            account_id=caller.id,
        )
    assert row is not None and row.pending_unsaved_work == "leave", (
        "the replacement happens at the caller's next message, so the answer has to be stored "
        "rather than lost with this turn"
    )


async def test_handoff_never_asks_about_uncommitted_work_when_no_repo_is_bound(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T_THREAD",
        account_id=caller.id,
        ma_session_id="sess_1",
        effective_config=_snapshot(repo_url=None),
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _hand_off_task_impl(
            runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
        )

    assert result.destination_name == _DESTINATION_NAME, (
        "a thread with nothing to lose is never interrupted with the question"
    )


@pytest.mark.parametrize("mismatch", ["caller", "tenant", "platform", "expired", "invalid"])
async def test_handoff_rejects_a_wrong_or_expired_origin_before_touching_ma(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    mismatch: str,
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="slack",
        parent_channel_id="C123",
        thread_id="123.456",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        now=now,
        expires_at=now + timedelta(minutes=-1 if mismatch == "expired" else 10),
    )
    await db_session.commit()
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="slack",
        platform_user_id="U1",
    )
    if mismatch == "caller":
        auth = replace(auth, account_id=uuid.uuid4())
    elif mismatch == "tenant":
        auth = replace(auth, tenant_id=uuid.uuid4())
    elif mismatch == "platform":
        auth = replace(auth, platform="discord")
    # No agent route registered: reaching MA at all would raise from the router.
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(MARouter().dispatch))

    with pytest.raises(ToolError, match="origin"):
        await _hand_off_task_impl(
            runtime,
            auth,
            origin_context_id="invalid" if mismatch == "invalid" else str(origin.id),
            agent_id=_DESTINATION_ID,
        )


async def test_fresh_start_marks_the_callers_live_session_without_tearing_it_down(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    mine = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T_THREAD",
        account_id=caller.id,
        ma_session_id="sess_mine",
    )
    theirs = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T_THREAD",
        account_id=other.id,
        ma_session_id="sess_theirs",
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(MARouter().dispatch))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _start_fresh_task_impl(runtime, auth, origin_context_id=str(origin.id))

    assert "Starting fresh from your next message here." in result.confirmation, (
        "the person is told what a fresh start leaves behind"
    )
    async with committing_sessionmaker() as session:
        still_live = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T_THREAD",
            account_id=caller.id,
        )
        neighbour = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T_THREAD",
            account_id=other.id,
        )
    assert still_live is not None and still_live.id == mine.id, (
        "nothing is removed until the new workspace is ready"
    )
    assert still_live.fresh_start_requested_at is not None, "the request is recorded for next bind"
    assert neighbour is not None and neighbour.id == theirs.id, (
        "a fresh start is caller-scoped and never touches another participant's session"
    )
    assert neighbour.fresh_start_requested_at is None, "and leaves their work alone"


async def test_fresh_start_confirms_even_when_the_caller_has_no_live_session(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing to retire is the same outcome the person asked for."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(MARouter().dispatch))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _start_fresh_task_impl(runtime, auth, origin_context_id=str(origin.id))

    assert "Starting fresh from your next message here." in result.confirmation, (
        "the confirmation does not depend on there being a session to retire"
    )


async def test_fresh_start_does_not_change_who_answers_in_the_thread(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_DESTINATION_ID,
        responder_name=_DESTINATION_NAME,
        kind="handoff",
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, build_fake_anthropic(MARouter().dispatch))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_DESTINATION_ID,
        responder_name=_DESTINATION_NAME,
        role=Role.USER,
    ) as origin:
        await _start_fresh_task_impl(runtime, auth, origin_context_id=str(origin.id))

    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
        )
        channels, tenants = await _count_routing_rows(session, tenant.id)
    assert binding is not None and binding.responder_ma_agent_id == _DESTINATION_ID, (
        "who answers here does not change"
    )
    assert (channels, tenants) == (0, 0), "and neither does channel or workspace routing"


async def test_handoff_with_an_answer_and_no_live_session_still_switches_the_responder(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A caller who has no session yet has no row to hold the answer. The
    switch must still happen: their first message starts a clean workspace,
    which is what an answer about uncommitted work would have governed."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _hand_off_task_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            agent_id=_DESTINATION_ID,
            unsaved_work="copy",
        )

    assert result.destination_ma_agent_id == _DESTINATION_ID
    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
        )
    assert binding is not None and binding.kind == "handoff", (
        "the responder switch is the part that must not depend on a session existing"
    )


async def test_handoff_refuses_an_agent_pinned_to_other_channels_and_writes_nothing(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A pinned agent is reachable (it answers in its own channels), which must
    not let anyone carry it into a conversation elsewhere."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={_DESTINATION_NAME: ("C_RX",)}),
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.ADMIN,
    ) as origin:
        with pytest.raises(ToolError, match="pinned to other channels"):
            await _hand_off_task_impl(
                runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
            )

    async with committing_sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
        )
    assert binding is None, "a refused handoff must leave the thread unbound"


async def test_handoff_admits_a_pinned_agent_inside_its_own_channel(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={_DESTINATION_NAME: ("C_RX",)}),
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client([_destination(tenant.id)]))
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_RX",
        thread_id="T_THREAD",
        responder_ma_agent_id=_RESPONDER_ID,
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        result = await _hand_off_task_impl(
            runtime, auth, origin_context_id=str(origin.id), agent_id=_DESTINATION_ID
        )

    assert result.destination_name == _DESTINATION_NAME
