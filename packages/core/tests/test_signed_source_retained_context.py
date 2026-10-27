"""A revoked signed-source result stays out of the next isolated session.

This models only the retention boundary. The synthetic source enforces
revocation on every call; the MA fake stores a previously returned MCP result.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsCustomSkill
from anthropic.types.beta.beta_managed_agents_mcp_toolset import BetaManagedAgentsMCPToolset
from anthropic.types.beta.sessions.beta_managed_agents_agent_mcp_tool_result_event import (
    BetaManagedAgentsAgentMCPToolResultEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.core import sessions
from daimon.core.config import McpSettings
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import MA_METADATA_KEY_CONTEXT_ISOLATED
from daimon.core.mcp_identity import relay_url
from daimon.core.specs import AgentSpec, dump_agent_spec
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import SessionBusyError
from daimon.core.turn.prepare import PreparedTurn, bind_session, isolated_relay_names
from daimon.core.workspace_transfer import WorkspaceTransferRunner
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_session_preparation import (
    _NOW,  # pyright: ignore[reportPrivateUsage]
    _admission,  # pyright: ignore[reportPrivateUsage]
    _deps,  # pyright: ignore[reportPrivateUsage]
    _register,  # pyright: ignore[reportPrivateUsage]
    _Transport,  # pyright: ignore[reportPrivateUsage]
)


def _source_agent(
    public_url: str,
) -> tuple[BetaManagedAgentsAgent, BetaManagedAgentsCustomSkill]:
    relay = relay_url(public_url, "signed-source")
    spec = AgentSpec.model_validate(
        {
            "name": "daimon",
            "model": "claude-sonnet-4-6",
            "mcp_servers": [{"type": "url", "name": "signed-source", "url": relay}],
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "signed-source"}],
        }
    )
    spec = spec.model_copy(
        update={
            "mcp_servers": merge_default_mcp_server(spec.mcp_servers, public_url),
            "tools": merge_default_mcp_toolset(spec.tools, public_url),
        }
    )
    provisioned = dump_agent_spec(spec, mode="json")
    skill = BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_private", version="v1")
    agent = ma_agent(
        id="ag_prep",
        name="daimon",
        model="claude-sonnet-4-6",
        mcp_servers=provisioned["mcp_servers"],
        tools=[
            {
                **tool,
                "configs": [
                    {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                        **config,
                    }
                    for config in tool.get("configs", [])
                ],
                "default_config": {"enabled": True, **tool.get("default_config", {})},
            }
            for tool in provisioned["tools"]
        ],
        skills=[skill],
    )
    return agent, skill


@pytest.mark.parametrize("suffix", ["signed-source?v=1", "%73igned-source"])
async def test_relay_url_variants_enter_isolated_admission(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    suffix: str,
) -> None:
    public_url = "https://daimon.example/mcp"
    url = relay_url(public_url, suffix)
    agent = ma_agent(
        mcp_servers=[{"type": "url", "name": "signed-source", "url": url}],
    )
    transport = _Transport()
    deps = replace(
        _deps(db_session_factory, transport),
        mcp=McpSettings(public_url=HttpUrl(public_url), jwt_secret=SecretStr("x" * 32)),
        public_url=public_url,
        isolated_relay_names=frozenset({"signed-source"}),
    )
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    admission = _admission(account=account, agent=agent)
    assert isolated_relay_names(deps, admission) == frozenset({"signed-source"})
    with pytest.raises(ValueError, match="dedicated relay-only"):
        await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
            now=lambda: _NOW,
        )
    assert transport.creates == 0


@pytest.mark.parametrize("provider_fault", ["server", "skill", "marker"])
async def test_revoked_source_result_is_absent_from_next_isolated_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    provider_fault: str,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    public_url = "https://daimon.example/mcp"
    agent, skill = _source_agent(public_url)
    _register(transport.state, agent)
    transport.state.ma.agents[agent.id]["skills"] = [skill.model_dump(mode="json")]
    assert {server.name for server in agent.mcp_servers} == {"signed-source", "daimon-mcp"}
    assert {tool.type for tool in agent.tools} == {"mcp_toolset", "agent_toolset_20260401"}
    assert agent.skills == [skill]
    deps = replace(
        _deps(db_session_factory, transport),
        mcp=McpSettings(public_url=HttpUrl(public_url), jwt_secret=SecretStr("x" * 32)),
        public_url=public_url,
        isolated_relay_names=frozenset({"signed-source"}),
    )
    admission = _admission(account=account, agent=agent)

    async def prepare():
        return await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
            now=lambda: _NOW,
        )

    async def fake_vault(*_args: object, **_kwargs: object) -> str:
        return "vlt_caller"

    async def fake_relay_vault(*_args: object, **_kwargs: object) -> str:
        return "vlt_relay"

    async def fake_archive_vault(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions, "ensure_agent_mcp_vault", fake_vault)
    monkeypatch.setattr(sessions, "create_relay_vault", fake_relay_vault)
    monkeypatch.setattr(sessions, "archive_vault", fake_archive_vault)

    source_allowed = True
    source_calls = 0
    retained_text = "synthetic-private-result"

    def ask_source() -> str:
        nonlocal source_calls
        source_calls += 1
        if not source_allowed:
            raise PermissionError("Source revoked")
        return retained_text

    first = await prepare()
    assert isinstance(first, PreparedTurn)
    result = BetaManagedAgentsAgentMCPToolResultEvent(
        id="sevt_source_result",
        type="agent.mcp_tool_result",
        mcp_tool_use_id="sevt_source_use",
        content=[BetaManagedAgentsTextBlock(type="text", text=ask_source())],
        is_error=False,
        processed_at=datetime(2026, 10, 27, tzinfo=UTC),
    )
    transport.state.events[first.ma_session_id].append(result.model_dump(mode="json"))
    source_allowed = False

    second = await prepare()
    assert isinstance(second, PreparedTurn)
    assert not second.reused and second.ma_session_id != first.ma_session_id
    assert source_calls == 1, "the second bind never asks the revoked source"
    assert any(
        event["type"] == "agent.mcp_tool_result"
        for event in transport.state.events[first.ma_session_id]
    )
    events = [
        event async for event in deps.anthropic.beta.sessions.events.list(second.ma_session_id)
    ]
    assert not any(event.type == "agent.mcp_tool_result" for event in events)
    frozen = transport.state.sessions[second.ma_session_id]
    assert frozen.resources == []
    assert frozen.metadata[MA_METADATA_KEY_CONTEXT_ISOLATED] == "true"
    assert frozen.vault_ids == ["vlt_relay"]
    assert [server.name for server in frozen.agent.mcp_servers] == ["signed-source"]
    assert len(frozen.agent.tools) == 1
    assert isinstance(frozen.agent.tools[0], BetaManagedAgentsMCPToolset)
    assert frozen.agent.tools[0].mcp_server_name == "signed-source"
    assert frozen.agent.skills == []

    original_create = sessions.create_session_record

    async def provider_ignores_override(*args: Any, **kwargs: Any):
        created = await original_create(*args, **kwargs)
        if provider_fault == "marker":
            return created.model_copy(
                update={
                    "metadata": {
                        key: value
                        for key, value in (created.metadata or {}).items()
                        if key != MA_METADATA_KEY_CONTEXT_ISOLATED
                    }
                }
            )
        if provider_fault == "skill":
            changed = created.agent.model_copy(update={"skills": [skill]})
        else:
            other = created.agent.mcp_servers[0].model_copy(
                update={"name": "transcript", "url": public_url}
            )
            changed = created.agent.model_copy(
                update={"mcp_servers": [*created.agent.mcp_servers, other]}
            )
        return created.model_copy(update={"agent": changed})

    monkeypatch.setattr(sessions, "create_session_record", provider_ignores_override)
    with pytest.raises(ValueError, match="provider did not honor isolated session"):
        await prepare()
    (rejected_id,) = set(transport.state.sessions) - {first.ma_session_id, second.ma_session_id}
    assert transport.state.sessions[rejected_id].archived_at is not None


@pytest.mark.parametrize("change", ["handoff", "relay_removed", "relay_removed_unreadable"])
async def test_leaving_isolated_source_never_checkpoints_or_carries_old_result(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    public_url = "https://daimon.example/mcp"
    source, skill = _source_agent(public_url)
    _register(transport.state, source)
    transport.state.ma.agents[source.id]["skills"] = [skill.model_dump(mode="json")]
    deps = replace(
        _deps(db_session_factory, transport),
        mcp=McpSettings(public_url=HttpUrl(public_url), jwt_secret=SecretStr("x" * 32)),
        public_url=public_url,
        isolated_relay_names=frozenset({"signed-source"}),
    )

    async def fake_vault(*_args: object, **_kwargs: object) -> str:
        return "vlt_caller"

    async def fake_relay_vault(*_args: object, **_kwargs: object) -> str:
        return "vlt_relay"

    async def fake_archive_vault(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sessions, "ensure_agent_mcp_vault", fake_vault)
    monkeypatch.setattr(sessions, "create_relay_vault", fake_relay_vault)
    monkeypatch.setattr(sessions, "archive_vault", fake_archive_vault)

    async def prepare(admission: Admission, current_deps: TurnDeps) -> PreparedTurn:
        return await bind_session(
            current_deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
            now=lambda: _NOW,
        )

    first = await prepare(_admission(account=account, agent=source), deps)
    assert isinstance(first, PreparedTurn)
    async with db_session_factory() as db:
        row = await get_live_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
        )
    assert row is not None and row.effective_config is not None
    assert row.effective_config.context_isolated
    old_result = BetaManagedAgentsAgentMCPToolResultEvent(
        id="sevt_source_retained",
        type="agent.mcp_tool_result",
        mcp_tool_use_id="sevt_source_use",
        content=[BetaManagedAgentsTextBlock(type="text", text="private-before-revocation")],
        is_error=False,
        processed_at=datetime(2026, 10, 27, tzinfo=UTC),
    )
    transport.state.events[first.ma_session_id].append(old_result.model_dump(mode="json"))

    destination = ma_agent(
        id="ag_successor" if change == "handoff" else source.id,
        name="daimon",
        model="claude-sonnet-4-6",
    )
    _register(transport.state, destination)
    binding_id = None
    if change == "handoff":
        async with db_session_factory() as db, db.begin():
            binding = await create_binding(
                db,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="channel-1",
                thread_id="thread-1",
                responder_ma_agent_id=destination.id,
                responder_name="daimon",
                kind="handoff",
            )
        binding_id = binding.id

    async def forbidden_checkpoint(self: WorkspaceTransferRunner, **kwargs: Any) -> Any:
        raise AssertionError("the isolated source must never execute a checkpoint")

    monkeypatch.setattr(WorkspaceTransferRunner, "__call__", forbidden_checkpoint)
    if change == "relay_removed_unreadable":
        transport.retrieve_error = 400
        with pytest.raises(SessionBusyError):
            await prepare(
                _admission(account=account, agent=destination),
                replace(deps, isolated_relay_names=frozenset()),
            )
        assert transport.creates == 1
        assert not transport.paths("POST", f"/v1/sessions/{first.ma_session_id}/events")
        return
    second = await prepare(
        _admission(account=account, agent=destination, thread_binding_id=binding_id),
        replace(deps, isolated_relay_names=frozenset()),
    )
    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id != first.ma_session_id and not second.reused
    assert second.continuity.transfer_kind is None
    assert second.continuity.user_prefix == ""
    assert transport.state.events[second.ma_session_id] == []
    assert not transport.paths("POST", f"/v1/sessions/{first.ma_session_id}/events")


@pytest.mark.parametrize("case", ["extra_server", "alias"])
async def test_isolated_agent_with_another_tool_route_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    case: str,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    public_url = "https://daimon.example/mcp"
    relay_server = {
        "type": "url",
        "name": "alternate" if case == "alias" else "signed-source",
        "url": relay_url(public_url, "signed-source"),
    }
    agent = ma_agent(
        id="ag_prep",
        name="daimon",
        model="claude-sonnet-4-6",
        mcp_servers=[
            relay_server,
            *(
                [{"type": "url", "name": "transcript", "url": public_url}]
                if case == "extra_server"
                else []
            ),
        ],
    )
    _register(transport.state, agent)
    deps = replace(
        _deps(db_session_factory, transport),
        mcp=McpSettings(public_url=HttpUrl(public_url), jwt_secret=SecretStr("x" * 32)),
        public_url=public_url,
        isolated_relay_names=frozenset({"signed-source"}),
    )
    with pytest.raises(ValueError, match="dedicated relay-only agent"):
        await bind_session(
            deps,
            _admission(account=account, agent=agent),
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
            now=lambda: _NOW,
        )
    assert transport.creates == 0
