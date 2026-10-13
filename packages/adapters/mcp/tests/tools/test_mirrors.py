"""Mirror tools and the `ask` hand-off: admin-only, Discord-only, rule refusals, audit."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import daimon.adapters.mcp.tools._ask_mirrors as _ask_mirrors
import daimon.adapters.mcp.tools.mirrors as tools
import discord
import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agent_chat import (
    AskResult,
    _ask_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.mirrors import MirrorOut
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import MirrorsSettings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import mirrors as mirrors_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import MirrorDeliveryRow, Role, TenantRow
from daimon.core.stores.security_audit import list_events
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import create_origin
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing import ma_agent, ma_model_config, ma_session, ma_session_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_create = tools._create_mirror_impl  # pyright: ignore[reportPrivateUsage]
_list = tools._list_mirrors_impl  # pyright: ignore[reportPrivateUsage]
_remove = tools._remove_mirror_impl  # pyright: ignore[reportPrivateUsage]
_NOW = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)


def _runtime(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    enabled: bool = True,
    router: MARouter | None = None,
) -> McpRuntime:
    agent = ma_agent(
        id="ag_helper",
        name="helper",
        model=ma_model_config("claude-sonnet-4-6", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: "helper"},
    )
    router = router or MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    client: AsyncAnthropic = build_fake_anthropic(router.dispatch)
    settings = MagicMock()
    settings.mirrors = MirrorsSettings(enabled=enabled)
    settings.artifacts = None
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.tool_safety = ToolSafetyPolicy(enabled=True)
    return McpRuntime(
        session_factory=sm,
        client=client,
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


async def _auth(
    session: AsyncSession, tenant: TenantRow, *, is_admin: bool = True, platform: str = "discord"
) -> AuthIdentity:
    return AuthIdentity(
        account_id=(await make_account(session, tenant=tenant)).id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform=platform,
        platform_user_id="9001",
        is_admin=is_admin,
    )


async def _create_in(runtime: McpRuntime, auth: AuthIdentity, dest: str, **kw: object) -> MirrorOut:
    return await _create(
        runtime,
        auth,
        name="qa",
        agent_name=str(kw.get("agent", "helper")),
        destination_id=dest,
        destination_kind="channel",
        consent_note=kw.get("consent"),  # type: ignore[arg-type]
        max_answer_chars=None,
    )


async def test_create_refuses_members_disabled_deployments_and_other_platforms(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    runtime, admin = _runtime(sessionmaker, tenant.id), await _auth(db_session, tenant)
    member = await _auth(db_session, tenant, is_admin=False)

    with pytest.raises(ToolError):
        await _create_in(runtime, member, "300")
    with pytest.raises(ToolError, match="switched off"):
        await _create_in(_runtime(sessionmaker, tenant.id, enabled=False), admin, "300")
    for platform in ("slack", "teams"):
        with pytest.raises(ToolError, match="only into Discord"):
            await _create_in(runtime, await _auth(db_session, tenant, platform=platform), "C0123")
    with pytest.raises(ToolError, match="no agent named"):
        await _create_in(runtime, admin, "300", agent="ghost")
    with pytest.raises(ToolError):
        await _create_in(runtime, admin, "not-a-snowflake")
    assert await mirrors_store.list_for_tenant(db_session, tenant.id) == [], "nothing saved"


async def test_create_refuses_a_home_kept_to_other_agents(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(channel_rules={"100": ChannelRule(readers="own", writers="own")}),
    )

    admin = await _auth(db_session, tenant)

    with pytest.raises(ToolError, match="Refused"):
        await _create_in(_runtime(sessionmaker, tenant.id), admin, "100")


async def test_create_checks_the_destinations_category(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(category_rules={"700": ChannelRule(writers="none")}),
    )
    admin = await _auth(db_session, tenant)
    categories = {300: 700, 301: 701}

    @contextlib.asynccontextmanager
    async def rest_client(_token: str) -> AsyncIterator[MagicMock]:
        client = MagicMock()

        async def fetch_channel(channel_id: int) -> discord.TextChannel:
            return MagicMock(spec=discord.TextChannel, category_id=categories[channel_id])

        client.fetch_channel = fetch_channel
        yield client

    monkeypatch.setattr(tools, "rest_client", rest_client)
    runtime = _runtime(sessionmaker, tenant.id)

    with pytest.raises(ToolError, match="lets nobody post"):
        await _create_in(runtime, admin, "300")
    assert (await _create_in(runtime, admin, "301")).destination_id == "301", "another category"


@pytest.mark.parametrize("caller", ["agent_key", "headless", "chat_turn_without_card"])
async def test_consent_is_refused_without_an_admins_approval_card(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession, caller: str
) -> None:
    tenant = await make_tenant(db_session)
    admin = await _auth(db_session, tenant)
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_helper")
    auth = {
        "agent_key": dataclasses.replace(admin, agent_id=agent_uuid),
        # An internal routine or headless token: admin, with no chat turn.
        "headless": admin,
        "chat_turn_without_card": dataclasses.replace(admin, chat_agent_id=agent_uuid),
    }[caller]

    with pytest.raises(ToolError, match="presses Approve on a card"):
        await _create_in(_runtime(sessionmaker, tenant.id), auth, "300", consent="they agreed")

    assert await mirrors_store.list_for_tenant(db_session, tenant.id) == [], "nothing saved"


async def test_consent_approved_on_a_card_is_recorded_audited_and_listed(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    admin = await _auth(db_session, tenant)
    now = dt.datetime.now(dt.UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=admin.account_id,
        platform="discord",
        parent_channel_id="500",
        thread_id="501",
        responder_ma_agent_id="ag_helper",
        responder_name="helper",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.ADMIN,
        expires_at=now + dt.timedelta(minutes=10),
        now=now,
    )
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="501",
        account_id=admin.account_id,
        ma_session_id="sesn_chat",
        ma_agent_id="ag_helper",
    )
    await db_session.commit()
    gated = {
        "type": "mcp_toolset",
        "mcp_server_name": "daimon-mcp",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        "configs": [
            {"name": "create_mirror", "enabled": True, "permission_policy": {"type": "always_ask"}}
        ],
    }
    chat = ma_session(id="sesn_chat", agent=ma_session_agent(id="ag_helper", tools=[gated]))
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/sesn_chat$",
        lambda _r, _m: httpx.Response(200, json=chat.model_dump(mode="json")),
    )
    runtime = _runtime(sessionmaker, tenant.id, router=router)
    auth = dataclasses.replace(
        admin, chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_helper")
    )

    created = await _create(
        runtime,
        auth,
        name="qa",
        agent_name="helper",
        destination_id="300",
        destination_kind="channel",
        consent_note="team agreed on 1 Oct",
        max_answer_chars=None,
        origin_context_id=str(origin.id),
    )

    assert created.consent is not None and "team agreed" in created.consent, created
    assert [m.name for m in await _list(runtime, auth)] == ["qa"], "listed"
    events = await list_events(db_session, tenant_id=tenant.id)
    assert [(e.tool_name, e.operation) for e in events] == [("create_mirror", "attest_consent")]
    assert "team agreed on 1 Oct" in events[0].reason, "the attestation is audited"
    assert await _remove(runtime, auth, "qa"), "remove reports success"
    assert await _list(runtime, auth) == [], "removed"


_SESSION = "sesn_1"


def _ma_routes(session_metadata: dict[str, str], status: Literal["idle", "running"]) -> MARouter:
    """A session of `helper` that answers "Answer" to whatever is sent."""
    router = MARouter()
    session = ma_session(
        id=_SESSION, agent_id="ag_helper", status=status, metadata=session_metadata
    ).model_dump(mode="json")
    message = {"type": "user.message", "content": [{"type": "text", "text": "Question"}]}
    answer = {"type": "agent.message", "content": [{"type": "text", "text": "Answer"}]}
    router.add("GET", r"/v1/sessions/([^/?]+)$", lambda _r, _m: httpx.Response(200, json=session))
    router.add(
        "POST",
        r"/v1/sessions/([^/?]+)/events",
        lambda _r, _m: list_response([{"id": "sevt_sent", **message, "processed_at": None}]),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/?]+)/events",
        lambda _r, _m: list_response(
            [{"id": "sevt_answer", **answer, "processed_at": "2026-10-01T12:00:01Z"}]
        ),
    )
    return router


async def _ask(
    runtime: McpRuntime, auth: AuthIdentity, *, timeout_seconds: float = 5.0, **kw: Any
) -> AskResult:
    """Continue `_SESSION` through the faked Managed Agents API."""
    return await _ask_impl(
        runtime,
        auth,
        "Question",
        handle=_SESSION,
        timeout_seconds=timeout_seconds,
        sleep=AsyncMock(),
        now=lambda: _NOW,
        **kw,
    )


async def _consented_mirror(
    session: AsyncSession,
    tenant: TenantRow,
    dest: str = "300",
    *,
    agent: str = "ag_helper",
    consent: str | None = "the askers agreed",
) -> None:
    await mirrors_store.upsert_mirror(
        session,
        tenant_id=tenant.id,
        name="qa",
        source_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent),
        source_agent_name="helper",
        source_agent_names=("helper",),
        dest_platform="discord",
        dest_kind="channel",
        dest_id=dest,
        consent_note=consent,
        max_answer_chars=None,
        account_id=None,
        now=_NOW,
    )
    await session.commit()


async def _caller(
    session: AsyncSession, tenant: TenantRow, *, bound_channel_id: str | None = None
) -> tuple[AuthIdentity, dict[str, str]]:
    """An agent key of `helper`, and the stamps of the session it opened."""
    account = await make_account(session, tenant=tenant)
    await session.commit()
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_helper"),
        bound_channel_id=bound_channel_id,
    )
    return auth, {MA_METADATA_KEY_ACCOUNT: str(account.id)}


async def _queued(session: AsyncSession) -> list[MirrorDeliveryRow]:
    return await mirrors_store.claim_mirror_deliveries(
        session,
        platform="discord",
        owner="t",
        now=_NOW,
        lease=dt.timedelta(minutes=1),
        max_pending_age=dt.timedelta(minutes=30),
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_ask_queues_the_answer_for_a_mirror_and_says_where(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession, enabled: bool
) -> None:
    tenant = await make_tenant(db_session)
    await _consented_mirror(db_session, tenant)
    caller, stamps = await _caller(db_session, tenant)
    router = _ma_routes(stamps, "idle")

    result = await _ask(_runtime(sessionmaker, tenant.id, enabled=enabled, router=router), caller)

    queued = await _queued(db_session)
    assert result.message == "Answer", result
    if enabled:
        assert result.shared_to == ("qa",), "the asker sees the mirror's name"
        assert queued[0].payload is not None and queued[0].payload.answer == "Answer", queued
    else:
        assert (result.shared_to, queued) == ((), []), "a disabled deployment queues nothing"


@pytest.mark.parametrize("case", ["no_consent", "other_agent", "mirror_step_timed_out"])
async def test_an_ask_a_mirror_does_not_take_still_answers(
    sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    tenant = await make_tenant(db_session)
    await _consented_mirror(
        db_session,
        tenant,
        agent="ag_other" if case == "other_agent" else "ag_helper",
        consent=None if case == "no_consent" else "the askers agreed",
    )
    caller, stamps = await _caller(db_session, tenant)
    if case == "mirror_step_timed_out":
        monkeypatch.setattr(_ask_mirrors, "_RECORD_TIMEOUT_S", 0)

    result = await _ask(
        _runtime(sessionmaker, tenant.id, router=_ma_routes(stamps, "idle")), caller
    )

    assert (result.message, result.shared_to) == ("Answer", ()), result
    assert await _queued(db_session) == [], "nothing is queued"


async def test_an_ask_that_times_out_queues_nothing(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    await _consented_mirror(db_session, tenant)
    caller, stamps = await _caller(db_session, tenant)
    runtime = _runtime(sessionmaker, tenant.id, router=_ma_routes(stamps, "running"))

    with pytest.raises(ToolError):
        await _ask(runtime, caller, timeout_seconds=0)

    assert await _queued(db_session) == [], "an unanswered ask is never mirrored"


async def test_a_session_sealed_when_it_ran_keeps_its_seal_after_the_channel_reopens(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    await _consented_mirror(db_session, tenant)
    caller, stamps = await _caller(db_session, tenant, bound_channel_id="200")
    router = _ma_routes({**stamps, "daimon_channel": "200", "daimon_sealed": "200"}, "idle")

    await _ask(_runtime(sessionmaker, tenant.id, router=router), caller)

    queued = await _queued(db_session)
    assert queued[0].payload is not None, queued
    assert queued[0].payload.source.seal_ids == ("200",), "the session's stamped seal is kept"
