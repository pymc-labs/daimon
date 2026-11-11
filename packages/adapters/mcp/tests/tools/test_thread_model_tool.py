"""`set_thread_model`: a person's own turn switches its conversation's model; nothing else can.

Real stores and real turn origins. Every refusal must leave no row, so a
routine, a wake or an agent-chat caller can never change the model the
people in a conversation chose.
"""

from __future__ import annotations

import uuid

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.thread_model import (
    _set_thread_model_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings, TurnSettings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_models import get_thread_model
from daimon.core.turn_origin import turn_origin
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_RESPONDER_ID = "agt_daimon"
_CHAT_SESSION = "chat-session-1"


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], *, mux: bool = False) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=AsyncAnthropic(api_key="test"),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
            turn=TurnSettings(path="mux", channel_backends=True) if mux else TurnSettings(),
        ),
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )


def _auth(
    tenant_id: uuid.UUID, account_id: uuid.UUID, *, chat_session_id: str | None = _CHAT_SESSION
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
        chat_session_id=chat_session_id,
    )


async def _stored(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, thread_id: str
) -> str | None:
    async with sessionmaker() as session:
        return await get_thread_model(
            session, tenant_id=tenant_id, platform="discord", thread_id=thread_id
        )


@pytest.mark.parametrize("origin_reason", ["chat", "github_access_ready", "private_input_applied"])
async def test_a_persons_turn_switches_its_own_conversation_only(
    origin_reason: str,
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)

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
        chat_session_id=_CHAT_SESSION,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        from datetime import UTC, datetime

        from daimon.core.stores.turn_origins import bind_origin_session, get_active_origin
        from daimon.core.turn.invocation import human_invocation

        async with committing_sessionmaker() as db:
            await bind_origin_session(
                db,
                origin_id=origin.id,
                chat_session_id=_CHAT_SESSION,
                attended=True,
                human_invocation=human_invocation(origin_reason, True),
            )
            current = await get_active_origin(
                db,
                origin_id=origin.id,
                tenant_id=tenant.id,
                account_id=caller.id,
                platform="discord",
                now=datetime.now(UTC),
            )
            assert current is not None and current.attended is True
            assert bool(current.human_invocation) is (origin_reason == "chat")
            await db.commit()
        result = await _set_thread_model_impl(
            runtime, _auth(tenant.id, caller.id), origin_context_id=str(origin.id), model="opus"
        )

    assert result.model == "claude-opus-5-5"
    assert result.note == "This conversation runs on Opus 5.5 from the next reply."
    assert await _stored(committing_sessionmaker, tenant.id, "T_THREAD") == "claude-opus-5-5"
    assert await _stored(committing_sessionmaker, tenant.id, "T_OTHER") is None


async def test_an_unattended_run_cannot_change_a_conversations_model(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A routine or wake running in the thread has an origin, but nobody is there."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)

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
        attended=False,
        chat_session_id=_CHAT_SESSION,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        with pytest.raises(ToolError, match="Only someone in the conversation"):
            await _set_thread_model_impl(
                runtime,
                _auth(tenant.id, caller.id),
                origin_context_id=str(origin.id),
                model="haiku",
            )

    assert await _stored(committing_sessionmaker, tenant.id, "T_THREAD") is None


@pytest.mark.parametrize("caller_session", [None, "another-session"], ids=["mcp_caller", "other"])
async def test_a_call_without_this_turns_origin_is_refused(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    caller_session: str | None,
) -> None:
    """An MCP or agent-chat caller has no chat grant; another session's origin isn't its own."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)

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
        chat_session_id=_CHAT_SESSION,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        with pytest.raises(ToolError):
            await _set_thread_model_impl(
                runtime,
                _auth(tenant.id, caller.id, chat_session_id=caller_session),
                origin_context_id=str(origin.id),
                model="opus",
            )

    assert await _stored(committing_sessionmaker, tenant.id, "T_THREAD") is None


async def test_a_model_this_deployment_does_not_offer_is_refused(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)

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
        chat_session_id=_CHAT_SESSION,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        with pytest.raises(ToolError, match="isn't a model this conversation can use"):
            await _set_thread_model_impl(
                runtime,
                _auth(tenant.id, caller.id),
                origin_context_id=str(origin.id),
                model="fable",
            )

    assert await _stored(committing_sessionmaker, tenant.id, "T_THREAD") is None


async def _openai_pick(monkeypatch, sessionmaker, tenant_id, account_id, *, admin: bool):
    """The conversation's agent on Claude, OpenAI allowed; the agent lookup is local."""
    from daimon.adapters.mcp.tools import thread_model
    from daimon.core.authz import Subject
    from daimon.core.channel_backend import AgentBackendService, BackendActor, BackendAgent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.thread_model_switch import ThreadPick

    service = AgentBackendService(sessionmaker, enabled=True)
    owner = BackendActor(tenant_id, account_id, Subject(is_admin=True))
    policy = await service.tenant_policy(tenant_id)
    await service.set_tenant_allowlist(tenant_id, ("anthropic", "openai"), policy.revision, owner)
    agent = BackendAgent(
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_RESPONDER_ID),
        name="daimon",
        configuration_digest="config",
    )
    actor = BackendActor(tenant_id, account_id, Subject(is_admin=admin))
    pick = ThreadPick(service, sessionmaker, None, agent, actor, _RESPONDER_ID, "discord", "42")

    async def local_pick(runtime, auth, origin):
        return pick

    monkeypatch.setattr(thread_model, "_pick", local_pick)
    return thread_model


async def test_another_providers_model_waits_for_the_persons_yes(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, mux=True)
    tool = await _openai_pick(
        monkeypatch, committing_sessionmaker, tenant.id, caller.id, admin=True
    )
    confirmed: list[tuple[str, str, str, int]] = []

    async def confirm(pick, value, *, thread_id, digest, revision):
        from daimon.core.thread_models import ThreadModelChange

        confirmed.append((value, thread_id, digest, revision))
        return ThreadModelChange(model_id="gpt-6-luna", note="kept")

    monkeypatch.setattr(tool, "confirm_thread_model", confirm)
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
        chat_session_id=_CHAT_SESSION,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        auth, where = _auth(tenant.id, caller.id), str(origin.id)
        shown = await _set_thread_model_impl(
            runtime, auth, origin_context_id=where, model="openai:gpt-6-luna"
        )
        assert shown.model == "openai:gpt-6-luna" and shown.digest is not None
        assert "Applies to: this conversation" in shown.changes
        assert confirmed == [], "nothing is kept before the person agrees"
        assert await _stored(committing_sessionmaker, tenant.id, "T_THREAD") is None
        kept = await _set_thread_model_impl(
            runtime,
            auth,
            origin_context_id=where,
            model="openai:gpt-6-luna",
            digest=shown.digest,
            revision=shown.revision,
        )
    assert kept.note == "kept"
    assert confirmed == [("openai:gpt-6-luna", "T_THREAD", shown.digest, shown.revision)]


async def test_another_providers_model_needs_the_owner_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, mux=True)
    await _openai_pick(monkeypatch, committing_sessionmaker, tenant.id, caller.id, admin=False)
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
        chat_session_id=_CHAT_SESSION,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        with pytest.raises(ToolError, match="owner or an admin can do that. Nothing changed."):
            await _set_thread_model_impl(
                runtime,
                _auth(tenant.id, caller.id),
                origin_context_id=str(origin.id),
                model="openai:gpt-6-luna",
            )
    assert await _stored(committing_sessionmaker, tenant.id, "T_THREAD") is None
