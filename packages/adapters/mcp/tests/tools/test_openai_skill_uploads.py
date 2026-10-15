"""Skill-upload confirmation and resource isolation on the OpenAI runtime."""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from daimon.adapters.mcp.tools.skill_uploads import (
    _add_skill_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.neutral_resources import HostAgentView, NeutralResourceHost
from daimon.core.skills.ingest import SkillBundle
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.neutral_catalog import AgentRevision
from fastmcp.exceptions import ToolError
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.resources import Agent
from mux.contracts.resources import AgentSpec as MuxAgentSpec
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_skill_uploads import (
    _MD,
    ROOM,
    _add_from,
    _cards_off_turn,
    _reply,
    _World,
    _world,
)

_OPENAI = BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna")
_GEMINI = BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")


def _no_native_io(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"native I/O the test did not expect: {request.url}")


class _OpenAIHost(NeutralResourceHost):
    """A real, scoped host whose catalog agent and Add edge are recorded stand-ins."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], scope: Scope) -> None:
        from daimon.testing.isolated_sdk import build_async_openai
        from mux.drivers.openai import OpenAIDriver
        from mux.drivers.openai.transport import SDKTransport
        from mux.drivers.openai.turn import MemoryRecoveryJournal
        from mux.drivers.openai.usage import MemoryUsageRevisions

        super().__init__(
            OpenAIDriver(
                SDKTransport(build_async_openai(transport=httpx.MockTransport(_no_native_io))),
                account_scope_id="project",
                authorization=lambda authorized, _kind, _id: authorized == scope,
                journal=MemoryRecoveryJournal(),
                usage_revisions=MemoryUsageRevisions(),
            ),
            sessions,
            scope=scope,
            account_scope_id="project",
            operation_key="test-operation",
        )
        self.row = AgentRevision(
            catalog_id="helper",
            local_revision=1,
            agent=Agent(
                ref=ResourceRef(
                    id="openai_helper",
                    kind="agent",
                    provider="openai",
                    account_scope_id="project",
                    tenant_id=scope.tenant_id,
                    account_id=scope.account_id,
                ),
                revision=Revision(local=0),
                created_at=datetime(2026, 10, 11, tzinfo=UTC),
                spec=MuxAgentSpec(
                    name="helper",
                    model=ModelRef(provider="openai", id="gpt-6-luna"),
                    metadata={"daimon_account": scope.account_id},
                ),
            ),
            principal_id=scope.principal_id,
        )
        self.added: list[tuple[str, str]] = []

    async def find(self, name: str) -> HostAgentView | None:
        return HostAgentView(self.row) if name == "helper" else None

    async def add(self, catalog_id: str, bundle: SkillBundle, *, recheck: Any) -> Any:
        await recheck(self.row.agent)
        self.added.append((catalog_id, bundle.preview.name))
        return SimpleNamespace(
            name=bundle.preview.name,
            skill_id="skill_openai_notes",
            version="1",
            action="created",
            newly_attached=True,
        )


@dataclass
class _Hosts:
    """The operator's factory: records requests, returns a host for any scope it is given."""

    sessions: async_sessionmaker[AsyncSession]
    foreign_account: bool = False
    account_scope_id: str = "project"
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    hosts: list[_OpenAIHost] = field(default_factory=list[_OpenAIHost])

    def __call__(self, **kwargs: Any) -> _OpenAIHost:
        self.calls.append(kwargs)
        account = uuid.uuid4() if self.foreign_account else kwargs["account_id"]
        host = _OpenAIHost(
            self.sessions,
            Scope(
                tenant_id=str(kwargs["tenant_id"]),
                account_id=str(account),
                principal_id=kwargs["principal_id"],
                authorization_id=kwargs["authorization_id"],
            ),
        )
        self.hosts.append(host)
        return host


async def _openai_room(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: BackendConfig = _OPENAI,
    authorize: bool = True,
    hosts: _Hosts | None = None,
) -> _Hosts:
    """ROOM configured for `config`, the operator's manifest authorizing it, and the capability."""
    async with world.runtime.session_factory.begin() as session:
        revision = await set_channel_backend(
            session, channel_ref(world.tenant_id, "discord", ROOM), config
        )
    if authorize:
        manifest = tmp_path / "deployments.json"
        manifest.write_text(
            json.dumps(
                [
                    {
                        "profile": "openai.persistent_workspace",
                        "tenant_id": str(world.tenant_id),
                        "platform": "discord",
                        "channel_id": ROOM,
                        "account_id": str(world.account_id),
                        "config_digest": revision.digest,
                        "project": "project",
                        "agent_id": "agent_deployed",
                        "environment_id": "env_deployed",
                        "api_key_env": "QA_UNSET_DEPLOYMENT_KEY",
                    }
                ]
            )
        )
        monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    else:
        monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    hosts = hosts or _Hosts(world.runtime.session_factory)
    world.runtime = dataclasses.replace(world.runtime, resource_hosts=cast(Any, hosts))
    return hosts


async def test_openai_cards_off_yes_adds_through_the_authorized_host(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    hosts = await _openai_room(world, monkeypatch, tmp_path)

    preview = await _add_skill_impl(
        world.runtime,
        auth,
        agent_name="helper",
        expected_ma_agent_id="openai_helper",
        skill_md=_MD,
        origin_context_id=origin,
    )
    assert preview.status == "preview" and "`yes`" in preview.summary
    async with _reply(world, auth, "yes") as reply:
        added = await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="openai_helper",
            skill_md=_MD,
            origin_context_id=reply,
            content_hash=preview.preview.content_hash,
        )
    assert added.status == "added" and added.added is not None
    assert added.added.skill_id == "skill_openai_notes"
    assert [host.added for host in hosts.hosts] == [[], [("helper", "notes")]]
    assert world.created == [], "nothing reached the Anthropic skill path"
    call = hosts.calls[-1]
    assert (call["tenant_id"], call["account_id"]) == (world.tenant_id, world.account_id)
    assert call["operation_key"].startswith("mcp-skill-add:")


@pytest.mark.parametrize("text,adds", [("yes please", True), ("yes please don't", False)])
async def test_openai_reads_a_polite_reply_like_every_backend(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text: str,
    adds: bool,
) -> None:
    """The shared matcher decides for every backend: a whole polite yes adds once
    through the authorized host, a qualified one cancels the preview."""
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    hosts = await _openai_room(world, monkeypatch, tmp_path)
    preview = await _add_skill_impl(
        world.runtime,
        auth,
        agent_name="helper",
        expected_ma_agent_id="openai_helper",
        skill_md=_MD,
        origin_context_id=origin,
    )

    async def confirm(reply: str) -> Any:
        return await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="openai_helper",
            skill_md=_MD,
            origin_context_id=reply,
            content_hash=preview.preview.content_hash,
        )

    async with _reply(world, auth, text) as reply:
        if adds:
            added = await confirm(reply)
            assert added.status == "added"
        else:
            with pytest.raises(ToolError, match="did not start with the person replying"):
                await confirm(reply)
    assert [host.added for host in hosts.hosts] == (
        [[], [("helper", "notes")]] if adds else [[], []]
    )
    assert world.created == [], "nothing reached the Anthropic skill path"


@pytest.mark.parametrize("case", ["no_capability", "no_deployment"])
async def test_an_openai_channel_without_the_capability_or_a_deployment_keeps_its_path(
    case: str,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    hosts = await _openai_room(world, monkeypatch, tmp_path, authorize=case == "no_capability")
    if case == "no_capability":
        world.runtime = dataclasses.replace(world.runtime, resource_hosts=None)
    preview = await _add_from(world, auth, origin)
    async with _reply(world, auth, "yes") as reply:
        added = await _add_from(world, auth, reply, content_hash=preview.preview.content_hash)
    assert added.status == "added"
    assert len(world.created) == 1 and world.created[0].endswith("helper/notes"), (
        "added on the existing Anthropic path"
    )
    assert hosts.calls == []


async def test_a_gemini_channel_add_is_refused_before_any_change(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    hosts = await _openai_room(world, monkeypatch, tmp_path, config=_GEMINI, authorize=False)
    with pytest.raises(ToolError, match="can't change an agent's skills"):
        await _add_from(world, auth, origin)
    assert hosts.calls == [] and world.created == []


async def test_a_host_for_another_account_is_refused_before_any_change(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    hosts = _Hosts(world.runtime.session_factory, foreign_account=True)
    await _openai_room(world, monkeypatch, tmp_path, hosts=hosts)
    with pytest.raises(ToolError, match="different tenant, account or workspace"):
        await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="openai_helper",
            skill_md=_MD,
            origin_context_id=origin,
        )
    assert all(host.added == [] for host in hosts.hosts) and world.created == []


async def _pending_rows(world: _World) -> int:
    async with world.runtime.session_factory() as session:
        count = await session.scalar(text("SELECT count(*) FROM pending_skill_adds"))
    return int(count or 0)


@pytest.mark.parametrize(
    ("expected", "refusal"),
    [(None, "Pass expected_ma_agent_id"), ("openai_someone_else", "missing or changed")],
)
async def test_an_openai_chat_add_must_name_the_current_agent(
    expected: str | None,
    refusal: str,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    hosts = await _openai_room(world, monkeypatch, tmp_path)
    with pytest.raises(ToolError, match=refusal):
        await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id=expected,
            skill_md=_MD,
            origin_context_id=origin,
        )
    assert all(host.added == [] for host in hosts.hosts) and await _pending_rows(world) == 0


async def test_an_agent_another_channel_keeps_to_itself_is_refused_alike_from_here(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Another channel reads only its own agents and "helper" runs there alone: from
    ROOM the add is refused before any preview, identically on either backend."""
    refusals: dict[str, str] = {}
    for backend in ("anthropic", "openai"):
        world = await _world(db_session_factory)
        elsewhere = "222222222222222222"
        # After the member's own grant, which would otherwise pin "helper" to ROOM.
        auth, origin = await _cards_off_turn(world)
        async with db_session_factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=world.tenant_id,
                policy=TenantAccessPolicy(
                    channel_rules={elsewhere: ChannelRule(readers="own", writers="own")},
                    agent_rules={"helper": AgentRule(runs_in=(elsewhere,))},
                ),
            )
        room = tmp_path / backend
        room.mkdir()
        hosts = await _openai_room(world, monkeypatch, room) if backend == "openai" else None
        with pytest.raises(ToolError) as refused:
            await _add_skill_impl(
                world.runtime,
                auth,
                agent_name="helper",
                expected_ma_agent_id="openai_helper" if backend == "openai" else "agent_helper",
                skill_md=_MD,
                origin_context_id=origin,
            )
        refusals[backend] = str(refused.value)
        assert world.created == [] and await _pending_rows(world) == 0
        if hosts is not None:
            assert all(host.added == [] for host in hosts.hosts)
    assert refusals["openai"] == refusals["anthropic"], "the same refusal on both backends"


async def test_an_agent_the_callers_view_hides_resolves_as_missing_behind_the_host(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """resolve_host_agent applies the caller's isolation view, as resolve_setup_agent does."""
    from daimon.adapters.mcp.tools import _rule_view

    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    hosts = await _openai_room(world, monkeypatch, tmp_path)
    seen: list[object] = []

    class _Hides:
        def sees_agent(self, agent: object) -> bool:
            seen.append(agent)
            return False

    async def hiding_view(*_args: object, **_kwargs: object) -> _Hides:
        return _Hides()

    monkeypatch.setattr(_rule_view, "load_isolation_view", hiding_view)
    with pytest.raises(ToolError, match="missing or changed"):
        await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="openai_helper",
            skill_md=_MD,
            origin_context_id=origin,
        )
    assert [getattr(agent, "name", None) for agent in seen] == ["helper"]
    assert all(host.added == [] for host in hosts.hosts) and await _pending_rows(world) == 0
