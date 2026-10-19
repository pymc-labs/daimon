"""Adapter delegation and UI safety against the published backend service contract.

The stub is a call recorder, not evidence of persistence or backend support.
"""

import json
import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.cli.commands import agents as cli
from daimon.adapters.cli.tenant import TenantSelector
from daimon.adapters.discord.agent_setup import details_view as dv
from daimon.adapters.discord.agent_setup import hydrate as dh
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import agents as mcp_agents
from daimon.adapters.slack.agent_setup import actions as sa
from daimon.adapters.slack.agent_setup import panel_views as sv
from daimon.adapters.slack.agent_setup.actions import load_details_view as read_slack_details
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
    encode_panel_metadata,
)
from daimon.adapters.teams import setup_card as tc
from daimon.adapters.teams import setup_panel as tp
from daimon.adapters.teams.card_actions import Actor
from daimon.core import channel_backend as cb
from daimon.core.agent_details import AgentDetails
from daimon.core.authz import Subject
from daimon.core.config import Settings, TurnSettings
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import security_audit
from daimon.core.stores.domain import Role
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import (
    make_account,
    make_cli_principal,
    make_platform_principal,
    make_tenant,
)
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import BaseModel
from rich.console import Console
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

TENANT = derive_tenant_uuid(platform="discord", workspace_id="2001")
ACCOUNT = uuid.uuid4()
AGENT = uuid.uuid4()


class Preview(BaseModel):
    tenant_id: uuid.UUID = TENANT
    agent_id: uuid.UUID = AGENT
    target_backend: str = "openai"
    setting_revision: int = 4
    policy_revision: str = "policy-digest"
    unsupported_skills: tuple[str, ...] = ("legacy skill",)
    unsupported_mcps: tuple[str, ...] = ("private connector",)
    unsupported_capabilities: tuple[str, ...] = ("native schedules",)
    shared_thread_limits: tuple[str, ...] = ("per caller only",)
    cost_line: str = "List price: unknown. Worst-case per turn: unknown."
    allowed: bool = True
    digest: str = "d" * 64
    # Mirrors `_BackendConsentFacts` on the real `CompatibilityPreview`: no
    # drops are declared in these stubs, so the CLI's drop-consent gate reads
    # the same "nothing to accept" shape it reads from the real preview.
    drop_consent: object | None = None

    @property
    def declared_drops(self) -> tuple[str, ...]:
        return () if self.drop_consent is None else self.drop_consent.declared_drops


@dataclass(frozen=True)
class BackendActor:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    subject: Subject


class Refused(DaimonError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Factory:
    @asynccontextmanager
    async def __call__(self):
        yield object()

    def __init__(self):
        self.begins = 0

    @asynccontextmanager
    async def begin(self):
        self.begins += 1
        yield object()


@pytest.fixture
def seam(monkeypatch):
    calls = []
    factory = Factory()
    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql://daimon:daimon@localhost:5432/daimon_test_stub"},
            "anthropic": {"api_key": "offline-owned"},
            "cli": {"local_user": "operator"},
            "turn": {"path": "mux", "channel_backends": True},
        }
    )
    agent = SimpleNamespace(
        tenant_id=TENANT,
        agent_id=AGENT,
        name="analyst",
        configuration_digest="current",
        model="claude-opus-5-5",
    )
    service = SimpleNamespace(enabled=True, _sessions=factory)
    service.preview = AsyncMock(
        side_effect=lambda actual, target: Preview(target_backend=target or DEFAULT_BACKEND)
    )
    service.current = AsyncMock(return_value=None)
    service.tenant_policy = AsyncMock(
        return_value=SimpleNamespace(enabled_backends=("anthropic", "openai"))
    )
    service.read_selection = cb.AgentBackendService.read_selection.__get__(service)
    service.refused = None

    async def change(actual, target, local, actor, *, expected_preview_digest):
        calls.append((actual, target, local, actor, expected_preview_digest))
        if service.refused:
            raise Refused(service.refused)
        return SimpleNamespace(backend=target, local=local + 1)

    async def clear(actual, local, actor, *, expected_preview_digest):
        return await change(
            actual, None, local, actor, expected_preview_digest=expected_preview_digest
        )

    service.set_backend = AsyncMock(side_effect=change)
    service.clear_backend = AsyncMock(side_effect=clear)
    loader = AsyncMock(return_value=agent)
    constructor = MagicMock(return_value=service)
    monkeypatch.setattr(
        cb,
        "backend_selection_enabled",
        lambda settings: settings.path == "mux" and settings.channel_backends,
        raising=False,
    )
    monkeypatch.setattr(cb, "make_backend_service", constructor, raising=False)
    monkeypatch.setattr(cb, "load_backend_agent", loader, raising=False)
    monkeypatch.setattr(cb, "BackendActor", BackendActor, raising=False)
    monkeypatch.setattr(cb, "AgentBackendRefused", Refused, raising=False)
    monkeypatch.setattr(cli, "resolve_tenant_override", AsyncMock(return_value=TENANT))
    monkeypatch.setattr(cli, "discover_tenant", AsyncMock(return_value=TENANT))
    monkeypatch.setattr(
        cli,
        "get_or_create_cli_principal",
        AsyncMock(return_value=SimpleNamespace(account_id=ACCOUNT, os_user="operator")),
    )
    monkeypatch.setattr(
        sa,
        "get_or_create_platform_principal",
        AsyncMock(return_value=SimpleNamespace(account_id=ACCOUNT)),
    )
    monkeypatch.setattr(sa, "resolve_is_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(sa, "record_panel_write", AsyncMock())
    monkeypatch.setattr(sa, "post_ephemeral", AsyncMock())
    monkeypatch.setattr(
        sa, "load_details_view", AsyncMock(return_value={"type": "modal", "blocks": []})
    )
    monkeypatch.setattr(dv, "record_panel_write", AsyncMock())
    monkeypatch.setattr(tp, "record_panel_write", AsyncMock())
    monkeypatch.setattr(tp, "get_or_create_account", AsyncMock(return_value=ACCOUNT))
    runtime = SimpleNamespace(
        settings=settings,
        sessionmaker=factory,
        session_factory=factory,
        anthropic=object(),
        client=object(),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    return SimpleNamespace(
        calls=calls,
        factory=factory,
        settings=settings,
        agent=agent,
        service=service,
        loader=loader,
        constructor=constructor,
        runtime=runtime,
    )


def details():
    return AgentDetails(
        ma_agent_id="ag_analyst",
        name="analyst",
        purpose="Analysis",
        model_id="claude-opus-5-5",
        model_display_name="Opus",
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        answers_in=(),
        answers_here=False,
        repo=None,
        skills=(),
        mcp_servers=(),
        keys=(),
        applies_note="Next message",
    )


def discord_view(seam, *, preview=None, clear=False):
    state = PanelState(
        roster=[],
        selected=None,
        account_id=ACCOUNT,
        guild_id=2001,
        is_admin=True,
        details=details(),
        backend_options=("anthropic", "openai"),
    )
    return dv.DetailsView(
        state,
        runtime=seam.runtime,
        allowed_user_id=42,
        details=state.details,
        backend_preview=preview,
        backend_clear=clear,
    )


def interaction(*, admin=True, user_id=42, guild_id=2001):
    event = MagicMock()
    event.user = MagicMock(spec=discord.Member)
    event.user.id = user_id
    event.user.guild_permissions.administrator = admin
    event.user.guild_permissions.manage_guild = False
    event.guild.owner_id = 999
    event.guild_id = guild_id
    event.response.is_done = MagicMock(return_value=False)
    event.response.defer = AsyncMock()
    event.response.send_message = AsyncMock()
    event.response.edit_message = AsyncMock()
    event.followup.send = AsyncMock()
    event.edit_original_response = AsyncMock()
    return event


@pytest.mark.parametrize("path,enabled", [("legacy", False), ("mux", False), ("legacy", True)])
async def test_flag_off_panels_and_mcp_registration_do_not_expose_backend_controls(
    seam, path, enabled
):
    seam.settings.turn = TurnSettings(path=path, channel_backends=enabled)
    view = discord_view(seam)
    assert not any(
        isinstance(item, discord.ui.Select) and item.placeholder == "Model backend"
        for item in view.walk_children()
    )
    mcp = FastMCP()
    mcp_agents.register_agent_tools(mcp, seam.runtime)
    assert "set_agent_backend" not in {tool.name for tool in await mcp.list_tools()}
    seam.loader.assert_not_awaited()
    seam.constructor.assert_not_called()


async def test_direct_mcp_tool_registered_once_when_enabled(seam):
    mcp = FastMCP()
    mcp_agents.register_agent_tools(mcp, seam.runtime)
    names = [tool.name for tool in await mcp.list_tools()]
    assert names.count("set_agent_backend") == 1
    assert names.count("request_agent_backend_switch") == 1


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini", None])
async def test_direct_mcp_uses_preview_and_reread_for_shared_service_cas(seam, backend):
    auth = AuthIdentity(account_id=ACCOUNT, tenant_id=TENANT, role=Role.ADMIN, is_admin=True)
    result = await mcp_agents._set_agent_backend_impl(
        seam.runtime, auth, "analyst", backend=backend, expected_revision=4
    )
    assert seam.loader.await_count == 2
    assert seam.calls[0][1:] == (
        backend,
        4,
        BackendActor(TENANT, ACCOUNT, Subject(is_admin=True)),
        "d" * 64,
    )
    assert result["revision"] == 5
    assert result["preview"]["cost_line"] == Preview().cost_line


@pytest.mark.parametrize("admin,agent_key", [(False, False), (True, True)])
async def test_direct_mcp_refuses_untrusted_admin_evidence_before_lookup(seam, admin, agent_key):
    auth = AuthIdentity(
        account_id=ACCOUNT,
        tenant_id=TENANT,
        role=Role.ADMIN,
        is_admin=admin,
        agent_id=AGENT if agent_key else None,
    )
    with pytest.raises(ToolError):
        await mcp_agents._set_agent_backend_impl(seam.runtime, auth, "analyst", backend="openai")
    seam.loader.assert_not_awaited()
    assert seam.calls == []


@pytest.mark.parametrize(
    "reason", ["stale_revision", "stale_preview", "backend_not_allowed", "tenant_mismatch"]
)
async def test_direct_mcp_preserves_shared_service_refusal(seam, reason):
    seam.service.refused = reason
    auth = AuthIdentity(account_id=ACCOUNT, tenant_id=TENANT, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match=reason):
        await mcp_agents._set_agent_backend_impl(seam.runtime, auth, "analyst", backend="openai")


async def test_chat_turn_cannot_bypass_confirmation_with_direct_mcp_tool(seam):
    auth = AuthIdentity(
        account_id=ACCOUNT, tenant_id=TENANT, role=Role.ADMIN, is_admin=True, chat_agent_id=AGENT
    )
    with pytest.raises(ToolError, match="request_agent_backend_switch"):
        await mcp_agents._set_agent_backend_impl(seam.runtime, auth, "analyst", backend="openai")
    seam.loader.assert_not_awaited()
    assert seam.calls == []


@pytest.mark.parametrize("backend", ["openai", "gemini", None])
async def test_cli_set_clear_uses_the_same_bound_preview_and_actor(seam, backend):
    console = Console(file=StringIO(), force_terminal=False, highlight=False)
    await cli.agents_backend_change(
        rt=seam.runtime, console=console, name="analyst", backend=backend, yes=True, as_json=True
    )
    value = json.loads(console.file.getvalue())
    assert value["backend"] == backend
    assert value["preview"]["unsupported_skills"] == ["legacy skill"]
    assert seam.loader.await_count == 2
    assert seam.calls[0][1] == backend
    assert seam.calls[0][2] == 4
    assert seam.calls[0][3].tenant_id == TENANT
    assert seam.calls[0][3].subject.is_admin
    assert seam.calls[0][4] == "d" * 64


@pytest.mark.parametrize(
    "admin,user,guild", [(False, 42, 2001), (True, 43, 2001), (True, 42, 2002)]
)
async def test_discord_confirm_rechecks_live_actor_and_tenant_before_lookup(
    seam, admin, user, guild
):
    view = discord_view(seam, preview=Preview())
    await view._on_backend_confirm(interaction(admin=admin, user_id=user, guild_id=guild))
    seam.loader.assert_not_awaited()
    assert seam.calls == []


@pytest.mark.parametrize("clear", [False, True])
async def test_discord_confirm_rereads_agent_and_forwards_bound_revision(seam, clear):
    view = discord_view(seam, preview=Preview(), clear=clear)
    view.swap_to = AsyncMock()
    await view._on_backend_confirm(interaction())
    assert seam.loader.await_count == 1
    assert seam.calls[0][1] == (None if clear else "openai")
    assert seam.calls[0][2] == 4
    assert seam.calls[0][4] == "d" * 64
    dv.record_panel_write.assert_awaited_once()
    assert dv.record_panel_write.await_args.kwargs["op"] == "agent_backend"


async def test_discord_expired_view_cannot_apply(seam):
    view = discord_view(seam, preview=Preview())
    view.stop()
    await view._on_backend_confirm(interaction())
    seam.loader.assert_not_awaited()
    assert seam.calls == []


async def test_discord_preview_renders_all_shared_facts(seam):
    view = discord_view(seam, preview=Preview())
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    for fact in (
        "legacy skill",
        "private connector",
        "native schedules",
        "per caller only",
        "List price",
        "Worst-case",
    ):
        assert fact in text


def slack_meta(**kwargs):
    return PanelMetadata(
        team_id="T1", channel_id="C1", view="details", agent_name="analyst", **kwargs
    )


async def slack_dispatch(seam, client, *, action_id, meta=None, action=None):
    await sa._dispatch_panel_action(
        seam.runtime,
        client,
        {"view": {"id": "V1", "hash": "current"}},
        action=action or {},
        action_id=action_id,
        meta=meta or slack_meta(),
        tenant_id=TENANT,
        team_id="T1",
        user_id="U1",
    )


async def test_slack_preview_binds_server_revision_digest_and_facts(seam):
    client = SimpleNamespace(views_update=AsyncMock())
    await slack_dispatch(
        seam,
        client,
        action_id=sv.ACTION_BACKEND_SELECT,
        action={"selected_option": {"value": "openai"}},
    )
    rendered = client.views_update.await_args.kwargs["view"]
    meta = decode_panel_metadata(rendered["private_metadata"])
    assert (meta.backend_revision, meta.backend_digest, meta.backend_target) == (
        4,
        "d" * 64,
        "openai",
    )
    assert "private connector" in json.dumps(rendered)
    assert seam.calls == []


async def test_slack_superseded_modal_refuses_before_service_mutation(seam):
    client = SimpleNamespace(
        views_update=AsyncMock(side_effect=SlackApiError("stale", {"error": "hash_conflict"}))
    )
    meta = slack_meta(backend_revision=4, backend_digest="d" * 64, backend_target="openai")
    await slack_dispatch(seam, client, action_id=sv.ACTION_BACKEND_CONFIRM, meta=meta)
    assert seam.calls == []
    assert seam.factory.begins == 0


async def test_slack_confirm_rereads_after_claim_and_rechecks_admin(seam):
    client = SimpleNamespace(views_update=AsyncMock())
    meta = slack_meta(backend_revision=4, backend_digest="d" * 64, backend_target="clear")
    await slack_dispatch(seam, client, action_id=sv.ACTION_BACKEND_CONFIRM, meta=meta)
    assert seam.loader.await_count == 2
    assert sa.resolve_is_admin.await_count == 2
    assert seam.calls[0][1] is None
    assert seam.calls[0][2] == 4
    assert seam.calls[0][4] == "d" * 64


def test_slack_metadata_defaults_stay_identical_and_malformed_revision_is_refused():
    raw = encode_panel_metadata(slack_meta())
    assert json.loads(raw) == {"t": "T1", "c": "C1", "v": "details", "a": "analyst"}
    assert decode_panel_metadata(json.dumps({**json.loads(raw), "br": True})) is None
    assert decode_panel_metadata(json.dumps({**json.loads(raw), "br": -1})) is None


async def test_teams_preview_uses_actual_shared_facts_and_bound_reference(seam, monkeypatch):
    actor = Actor(user_id="person", tenant_id=TENANT, is_admin=True, conversation_id="chat")
    monkeypatch.setattr(tp, "card_actor", AsyncMock(return_value=actor))
    data = {"op": "backend_preview", "agent": "analyst", "model_backend": "openai"}
    ctx = SimpleNamespace(
        activity=SimpleNamespace(value=SimpleNamespace(action=SimpleNamespace(data=data)))
    )
    panel = tp.SetupPanel(seam.runtime)
    result = await panel._act(ctx)
    text = result.model_dump_json()
    assert "private connector" in text
    assert '"revision":4' in text
    assert "d" * 64 in text
    assert seam.calls == []


async def test_teams_non_admin_confirmation_refuses_before_lookup(seam, monkeypatch):
    actor = Actor(user_id="person", tenant_id=TENANT, is_admin=False, conversation_id="chat")
    monkeypatch.setattr(tp, "card_actor", AsyncMock(return_value=actor))
    ctx = SimpleNamespace(
        activity=SimpleNamespace(
            value=SimpleNamespace(
                action=SimpleNamespace(data={"op": "backend_confirm", "agent": "analyst"})
            )
        )
    )
    await tp.SetupPanel(seam.runtime)._act(ctx)
    seam.loader.assert_not_awaited()
    assert seam.calls == []


async def test_teams_confirmation_rereads_actor_and_agent(seam, monkeypatch):
    actor = Actor(user_id="person", tenant_id=TENANT, is_admin=True, conversation_id="chat")
    actor_read = AsyncMock(return_value=actor)
    monkeypatch.setattr(tp, "card_actor", actor_read)
    panel = tp.SetupPanel(seam.runtime)
    panel._details = AsyncMock(return_value=tc.notice_card("Updated"))
    data = {
        "op": "backend_confirm",
        "agent": "analyst",
        "target": "openai",
        "revision": 4,
        "digest": "d" * 64,
    }
    ctx = SimpleNamespace(
        activity=SimpleNamespace(value=SimpleNamespace(action=SimpleNamespace(data=data)))
    )
    await panel._act(ctx)
    assert actor_read.await_count == 2
    assert seam.loader.await_count == 2
    assert seam.calls[0][1] == "openai"
    assert seam.calls[0][2] == 4
    assert seam.calls[0][4] == "d" * 64


def test_slack_teams_unconfigured_renderers_keep_backend_control_absent():
    slack = sv.build_details_view(
        details(),
        meta=slack_meta(),
        is_admin=True,
        coding_tools_available=False,
        channel_id="C1",
        attribution=None,
    )
    teams = tc.details_card(details(), here="chat", page=0, coding_tools=False)
    assert "Model backend" not in json.dumps(slack)
    assert "Model backend" not in teams.model_dump_json()


@pytest.fixture
async def persisted(
    db_preflight: DatabasePreflight, db_nullpool_engine: AsyncEngine, db_clean: None
):
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session, workspace_id="2001")
        account = await make_account(session, tenant=tenant)
        await make_cli_principal(session, tenant=tenant, account=account, os_user="operator")
        await make_platform_principal(
            session, tenant=tenant, account=account, platform="discord", external_id="42"
        )
    settings = Settings.model_validate(
        {
            "database": {"url": str(db_nullpool_engine.url)},
            "anthropic": {"api_key": "offline-owned"},
            "cli": {"local_user": "operator"},
            "turn": {"path": "mux", "channel_backends": True},
        }
    )
    service = cb.make_backend_service(sessions, settings.turn)
    actor = cb.BackendActor(tenant.id, account.id, Subject(is_admin=True))
    policy = await service.tenant_policy(tenant.id)
    await service.set_tenant_allowlist(
        tenant.id, ("anthropic", "openai", "gemini"), policy.revision, actor
    )
    native = ma_agent(
        id="actual-owned-agent", tenant_id=str(tenant.id), name="analyst", model="claude-opus-5-5"
    ).model_dump(mode="json")
    native["metadata"]["daimon_name"] = "analyst"
    # Trusted source capture (DD3) additionally reads the real target agent and
    # its deployment environment, as production does: `defaults/config.yaml`
    # ships a non-empty `environment_name`, so the fixture's deployment
    # default carries one too, with a matching environment resource for
    # `find_environment_by_daimon_tag` to find.
    environment = ma_environment(tenant_id=tenant.id)
    environment.metadata["daimon_name"] = "default"
    router = MARouter()
    router.add("GET", rf"/v1/agents/{native['id']}", lambda *_: httpx.Response(200, json=native))
    router.add(
        "GET",
        rf"/v1/environments/{environment.id}",
        lambda *_: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    router.add(
        "GET", r"/v1/environments", lambda *_: list_response([environment.model_dump(mode="json")])
    )
    return SimpleNamespace(
        sessions=sessions,
        tenant=tenant,
        account=account,
        settings=settings,
        service=service,
        native=native,
        environment=environment,
        router=router,
    )


def replies(trace, native, count):
    for _ in range(count):
        trace.queue(ScriptedReply("GET", "/v1/agents", list_response([native])))


def runtime(persisted, client):
    return SimpleNamespace(
        settings=persisted.settings,
        sessionmaker=persisted.sessions,
        session_factory=persisted.sessions,
        anthropic=client,
        client=client,
        deployment_default=DeploymentDefault(agent_name="analyst", environment_name="default"),
        resolver_cache=new_resolver_cache(),
    )


#: DD3's trusted source capture (`capture_backend_source`) runs the same
#: obligation check the workers use: this fixture agent has no explicit
#: tools, skills or MCPs, so the only capabilities every real Managed Agent
#: implicitly carries -- memory and its sandbox/environment -- are what a
#: cross-profile switch (anthropic -> openai/gemini) must declare dropped.
#: Anthropic keeps full fidelity (same profile), so it declares nothing.
_CROSS_PROFILE_DROPS = ("memory", "sandbox")


@pytest.mark.parametrize("surface", ["cli", "mcp"])
@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_direct_surfaces_persist_set_clear_and_atomic_audit(persisted, surface, backend):
    drops = () if backend == "anthropic" else _CROSS_PROFILE_DROPS
    trace = ScriptedTransport(router=persisted.router)
    replies(trace, persisted.native, 4)
    async with trace.client() as client:
        rt = runtime(persisted, client)
        auth = AuthIdentity(
            tenant_id=persisted.tenant.id,
            account_id=persisted.account.id,
            role=Role.ADMIN,
            is_admin=True,
        )
        if surface == "mcp" and drops:
            # The headless `set_agent_backend` MCP tool has no human to ask for
            # consent (that is exactly what `request_agent_backend_switch`'s
            # chat Confirm card is for), so a target that needs declared drops
            # always refuses here -- never a silent drop, never a guessed
            # accept.
            with pytest.raises(ToolError, match="drops required"):
                await mcp_agents._set_agent_backend_impl(
                    rt, auth, "analyst", backend=backend, expected_revision=0
                )
            async with persisted.sessions() as session:
                rows = await security_audit.list_events(session, tenant_id=persisted.tenant.id)
            assert [row.operation for row in rows] == ["allowlist"]
            assert [(r.method, r.path) for r in trace.requests] == [
                ("GET", "/v1/agents"),
                ("GET", "/v1/agents/actual-owned-agent"),
                ("GET", "/v1/environments"),
                ("GET", "/v1/agents/actual-owned-agent"),
                ("GET", "/v1/environments/env_test"),
                ("GET", "/v1/agents"),
                ("GET", "/v1/agents/actual-owned-agent"),
                ("GET", "/v1/environments"),
                ("GET", "/v1/agents/actual-owned-agent"),
                ("GET", "/v1/environments/env_test"),
            ]
            assert not trace.violations
            return
        if surface == "cli":
            for target in (backend, None):
                console = Console(file=StringIO(), force_terminal=False, highlight=False)
                await cli.agents_backend_change(
                    rt=rt,
                    console=console,
                    name="analyst",
                    backend=target,
                    yes=True,
                    as_json=True,
                    selector=TenantSelector(tenant_id=str(persisted.tenant.id)),
                    accept_drops=",".join(drops) if target is not None and drops else None,
                )
                assert json.loads(console.file.getvalue())["backend"] == target
        else:
            for local, target in enumerate((backend, None)):
                result = await mcp_agents._set_agent_backend_impl(
                    rt, auth, "analyst", backend=target, expected_revision=local
                )
                assert result["backend"] == target and result["revision"] == local + 1
    # Independent store reads, not the result returned by the write.
    async with persisted.sessions() as session:
        rows = await security_audit.list_events(session, tenant_id=persisted.tenant.id)
    writes = [row for row in rows if row.operation in ("set", "clear")]
    assert [row.operation for row in writes] == ["set", "clear"]
    assert all(
        row.account_id == persisted.account.id and row.outcome == "allowed" for row in writes
    )
    agent = cb.BackendAgent(
        tenant_id=persisted.tenant.id,
        agent_id=writes[0].agent_id,
        name="analyst",
        configuration_digest="unused-read-only",
    )
    current = await persisted.service.current(agent)
    assert current.local == 2 and current.backend is None and current.previous_local == 1
    previous = await persisted.service.revision(agent, 1)
    assert previous.backend == backend
    # DD3's trusted source capture reads the real target agent and its
    # deployment environment on every "set" preview (never on "clear"): once
    # from the top-level `service.preview`, once more from `_change`'s own
    # re-preview just before the write. Both are plain read-only GETs; "clear"
    # makes none.
    capture = [
        ("GET", "/v1/agents/actual-owned-agent"),
        ("GET", "/v1/environments"),
        ("GET", "/v1/agents/actual-owned-agent"),
        ("GET", "/v1/environments/env_test"),
    ]
    assert [(r.method, r.path) for r in trace.requests] == [
        ("GET", "/v1/agents"),
        *capture,
        ("GET", "/v1/agents"),
        *capture,
        ("GET", "/v1/agents"),
        ("GET", "/v1/agents"),
    ]
    assert not trace.replies and not trace.violations


async def test_changed_actual_sdk_agent_refuses_direct_write_without_audit(persisted):
    # DD3's trusted source capture re-reads the real target agent by id on
    # every preview (not the list response `load_backend_agent` used before
    # DD3), so the race this test proves is now visible there: the first
    # capture (inside the initial `service.preview`) sees the original
    # agent; the second capture (inside `_change`, just before the write)
    # sees it changed, which must still land as a digest mismatch refusal.
    changed = {**persisted.native, "description": "Changed after the preview"}
    calls = {"agent": 0}

    def get_agent(*_):
        # Each capture reads the agent by id twice (once directly, once more
        # inside `capture_preparation_source`'s own fresh read): keep the
        # first capture entirely on the original agent and only flip to the
        # changed one once the first capture has fully completed, so the
        # mutation lands cleanly between the two captures, not inside one.
        calls["agent"] += 1
        return httpx.Response(200, json=persisted.native if calls["agent"] <= 2 else changed)

    router = MARouter()
    router.add("GET", rf"/v1/agents/{persisted.native['id']}", get_agent)
    router.add(
        "GET",
        rf"/v1/environments/{persisted.environment.id}",
        lambda *_: httpx.Response(200, json=persisted.environment.model_dump(mode="json")),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda *_: list_response([persisted.environment.model_dump(mode="json")]),
    )
    trace = ScriptedTransport(router=router)
    replies(trace, persisted.native, 2)
    async with trace.client() as client:
        auth = AuthIdentity(
            tenant_id=persisted.tenant.id,
            account_id=persisted.account.id,
            role=Role.ADMIN,
            is_admin=True,
        )
        # Anthropic (same profile as the source): no drops are declared, so
        # this exercises the digest-staleness refusal alone, independent of
        # the drop-consent gate exercised elsewhere.
        with pytest.raises(ToolError, match="stale preview"):
            await mcp_agents._set_agent_backend_impl(
                runtime(persisted, client), auth, "analyst", backend="anthropic"
            )
    async with persisted.sessions() as session:
        rows = await security_audit.list_events(session, tenant_id=persisted.tenant.id)
    assert [row.operation for row in rows] == ["allowlist"]
    assert not trace.replies and not trace.violations


@pytest.mark.parametrize("clear", [False, True])
async def test_discord_confirm_records_shared_and_panel_audit(persisted, clear):
    trace = ScriptedTransport()
    replies(trace, persisted.native, 2)
    async with trace.client() as client:
        agent = await cb.load_backend_agent(
            client, persisted.sessions, tenant_id=persisted.tenant.id, agent_name="analyst"
        )
        preview = await persisted.service.preview(agent, "anthropic" if clear else "gemini")
        state = PanelState(
            roster=[],
            selected=None,
            account_id=persisted.account.id,
            guild_id=2001,
            is_admin=True,
            details=details(),
            backend_options=("anthropic", "openai", "gemini"),
        )
        view = dv.DetailsView(
            state,
            runtime=runtime(persisted, client),
            allowed_user_id=42,
            details=state.details,
            backend_preview=preview,
            backend_clear=clear,
        )
        view.swap_to = AsyncMock()
        await view._on_backend_confirm(interaction())
        view.swap_to.assert_awaited_once()
    current = await persisted.service.current(agent)
    assert current.local == 1 and current.backend == (None if clear else "gemini")
    async with persisted.sessions() as session:
        rows = await security_audit.list_events(session, tenant_id=persisted.tenant.id)
    write = next(
        row for row in rows if row.tool_name == "agent_backend" and row.operation != "allowlist"
    )
    panel = next(row for row in rows if row.tool_name == "panel:agent_backend")
    assert write.account_id == panel.account_id == persisted.account.id
    assert write.outcome == panel.outcome == "allowed"
    assert panel.platform == "discord" and panel.platform_user_id == "42"
    assert not trace.replies and not trace.violations


@pytest.mark.parametrize("path,enabled", [("legacy", False), ("mux", False), ("legacy", True)])
async def test_cli_disabled_refuses_before_database_or_agent_lookup(seam, path, enabled):
    seam.settings.turn = TurnSettings(path=path, channel_backends=enabled)
    with pytest.raises(cli.StoreError, match="disabled"):
        await cli.agents_backend_change(
            rt=seam.runtime,
            console=Console(file=StringIO()),
            name="analyst",
            backend="openai",
            yes=True,
            as_json=True,
        )
    assert seam.factory.begins == 0
    seam.loader.assert_not_awaited()
    seam.constructor.assert_not_called()


async def test_mcp_set_clear_schema_requires_explicit_target(seam):
    mcp = FastMCP()
    mcp_agents.register_agent_tools(mcp, seam.runtime)
    tool = next(tool for tool in await mcp.list_tools() if tool.name == "set_agent_backend")
    assert "backend" in tool.parameters["required"]
    assert {
        alternative.get("type") for alternative in tool.parameters["properties"]["backend"]["anyOf"]
    } == {"string", "null"}


async def test_large_preview_lists_keep_cost_and_thread_limits_visible(seam):
    preview = Preview(
        unsupported_skills=("large skill " * 200,), unsupported_mcps=("large connector " * 200,)
    )
    view = discord_view(seam, preview=preview)
    discord_text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    slack_text = json.dumps(sv.build_backend_preview_view(preview, meta=slack_meta()))
    teams_text = json.dumps(
        tc.backend_preview_card(preview, agent_name="analyst", clear=False).model_dump(mode="json")
    )
    for text in (discord_text, slack_text, teams_text):
        assert "truncated" in text
        assert "List price: unknown" in text
        assert "per caller only" in text
        assert "Existing threads keep their backend" in text


async def test_missing_actual_sdk_agent_is_a_typed_mcp_refusal(persisted):
    trace = ScriptedTransport()
    trace.queue(ScriptedReply("GET", "/v1/agents", list_response([])))
    async with trace.client() as client:
        auth = AuthIdentity(
            tenant_id=persisted.tenant.id,
            account_id=persisted.account.id,
            role=Role.ADMIN,
            is_admin=True,
        )
        with pytest.raises(ToolError, match="agent not found"):
            await mcp_agents._set_agent_backend_impl(
                runtime(persisted, client), auth, "missing", backend="openai"
            )
    async with persisted.sessions() as session:
        rows = await security_audit.list_events(session, tenant_id=persisted.tenant.id)
    assert [row.operation for row in rows] == ["allowlist"]
    assert not trace.replies and not trace.violations


def cli_registration_probe(tmp_path, *, path, enabled):
    # A fresh process witnesses the public command tree, with no ambient settings
    # or local .env. Help needs neither a database nor provider credentials.
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "COLUMNS": "80", "LINES": "24"}
    if path is not None:
        env["DAIMON_TURN__PATH"] = path
    if enabled is not None:
        env["DAIMON_TURN__CHANNEL_BACKENDS"] = enabled
    program = """
import click
import json
from daimon.adapters.cli.commands.agents import agents_app
from daimon.adapters.cli.main import app
from typer.main import get_command
from typer.testing import CliRunner
runner = CliRunner()
help_result = runner.invoke(agents_app, ["--help"])
backend_result = runner.invoke(agents_app, ["backend", "--help"])
root_result = runner.invoke(app, ["agents", "--help"])
command = get_command(agents_app)
context = click.Context(command)
print(json.dumps({"groups": [name for name in command.list_commands(context)
    if isinstance(command.get_command(context, name), click.Group)],
    "help": help_result.stdout, "exit": help_result.exit_code,
    "backend_help": backend_result.stdout, "backend_exit": backend_result.exit_code,
    "backend_output": backend_result.output,
    "root_help": root_result.stdout, "root_exit": root_result.exit_code}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "path,enabled", [(None, None), ("legacy", "false"), ("mux", "false"), ("legacy", "true")]
)
def test_disabled_cli_retains_exact_legacy_command_tree_and_help(tmp_path, path, enabled):
    actual = cli_registration_probe(tmp_path, path=path, enabled=enabled)
    # Actual unchanged-parent CliRunner stdout, captured by the independent
    # review at 9152b5a; no runtime dependence on Git history/shallow CI clones.
    reference = json.loads(
        (Path(__file__).parent / "data/agent_backend_cli_legacy_help.json").read_text()
    )
    assert actual["exit"] == actual["root_exit"] == 0
    assert actual["groups"] == ["rule", "tool-rules"]
    # This checks that enabling the backend feature does not alter the
    # pre-existing CLI surface. The tool-rules command is independently added.
    tool_rule_lines = [
        line for line in actual["help"].splitlines(keepends=True) if "│ tool-rules " in line
    ]
    assert len(tool_rule_lines) == 1
    assert actual["help"].replace(tool_rule_lines[0], "") == reference["stdout"]
    assert actual["backend_exit"] == 2
    assert [
        line.strip("│ ")
        for line in actual["backend_output"].splitlines()
        if "No such command" in line
    ] == ["No such command 'backend'."]
    assert "Did you mean" not in actual["backend_output"]
    assert "backend" not in actual["root_help"]


def test_enabled_cli_registers_backend_set_clear_once(tmp_path):
    actual = cli_registration_probe(tmp_path, path="mux", enabled="true")
    assert actual["exit"] == actual["root_exit"] == actual["backend_exit"] == 0
    assert actual["groups"] == ["rule", "backend", "tool-rules"]
    assert "backend" in actual["help"] and "backend" in actual["root_help"]
    assert "set" in actual["backend_help"] and "clear" in actual["backend_help"]


@pytest.mark.parametrize(
    "arguments", [["--help"], ["version"], ["run", "--help"], ["help", "agents"]]
)
def test_unrelated_cli_commands_do_not_parse_malformed_turn_settings(tmp_path, arguments):
    program = """
import json, sys
from daimon.adapters.cli.main import app
from typer.testing import CliRunner
result = CliRunner().invoke(app, json.loads(sys.argv[1]))
print(json.dumps({"exit": result.exit_code, "output": result.output,
                  "exception": repr(result.exception)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, json.dumps(arguments)],
        cwd=tmp_path,
        env={"PATH": os.defpath, "DAIMON_TURN__CHANNEL_BACKENDS": "not-a-boolean"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    actual = json.loads(result.stdout)
    assert actual["exit"] == 0, actual
    assert actual["output"].strip()
    assert "ValidationError" not in actual["exception"]


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini", None])
async def test_direct_mcp_preview_only_reads_without_mutation_or_source_reread(seam, backend):
    auth = AuthIdentity(account_id=ACCOUNT, tenant_id=TENANT, role=Role.ADMIN, is_admin=True)
    result = await mcp_agents._set_agent_backend_impl(
        seam.runtime,
        auth,
        "analyst",
        backend=backend,
        preview_only=True,
    )
    assert result["applied"] is False
    assert result["preview"]["target_backend"] == (backend or "anthropic")
    assert "revision" not in result
    assert seam.loader.await_count == 1
    assert seam.calls == []
    seam.service.set_backend.assert_not_awaited()
    seam.service.clear_backend.assert_not_awaited()


@pytest.mark.parametrize("context", ["chat_agent_id", "slack_turn_context_id"])
@pytest.mark.parametrize("preview_only", [False, True])
async def test_direct_mcp_refuses_each_chat_credential_before_any_lookup(
    seam, context, preview_only
):
    auth = AuthIdentity(
        account_id=ACCOUNT,
        tenant_id=TENANT,
        role=Role.ADMIN,
        is_admin=True,
        **{context: uuid.uuid4()},
    )
    with pytest.raises(ToolError, match="request_agent_backend_switch"):
        await mcp_agents._set_agent_backend_impl(
            seam.runtime,
            auth,
            "analyst",
            backend="openai",
            preview_only=preview_only,
        )
    seam.loader.assert_not_awaited()
    assert seam.calls == []


async def test_discord_stale_selection_refuses_without_losing_details(seam):
    seam.service.read_selection = AsyncMock(return_value=None)
    view = discord_view(seam)
    selected = SimpleNamespace(values=["openai"])
    event = interaction()
    view.swap_to = AsyncMock()
    await view._on_backend_select(event, select=selected)
    event.followup.send.assert_awaited_once()
    assert "unavailable" in event.followup.send.await_args.args[0]
    view.swap_to.assert_not_awaited()
    assert seam.calls == []


async def test_slack_stale_selection_refuses_without_mutating_or_replacing_modal(seam):
    seam.service.read_selection = AsyncMock(return_value=None)
    client = SimpleNamespace(views_update=AsyncMock())
    await slack_dispatch(
        seam,
        client,
        action_id=sv.ACTION_BACKEND_SELECT,
        action={"selected_option": {"value": "openai"}},
        meta=PanelMetadata(
            team_id="T1",
            channel_id="C1",
            agent_name="analyst",
            view="details",
        ),
    )
    client.views_update.assert_not_awaited()
    sa.post_ephemeral.assert_awaited_once()
    assert "unavailable" in sa.post_ephemeral.await_args.kwargs["text"]
    assert seam.calls == []


async def test_teams_stale_selection_returns_toast_without_mutation(seam, monkeypatch):
    seam.service.read_selection = AsyncMock(return_value=None)
    actor = Actor(user_id="admin", tenant_id=TENANT, is_admin=True, conversation_id="conversation")
    monkeypatch.setattr(tp, "card_actor", AsyncMock(return_value=actor))
    ctx = SimpleNamespace(
        activity=SimpleNamespace(
            value=SimpleNamespace(
                action=SimpleNamespace(
                    data={"op": "backend_preview", "agent": "analyst", "model_backend": "openai"},
                )
            )
        )
    )
    response = await tp.SetupPanel(seam.runtime)._act(ctx)
    assert "unavailable" in str(response)
    assert seam.calls == []


async def test_details_hydration_hides_unavailable_backend_controls(seam, monkeypatch):
    seam.service.read_selection = AsyncMock(return_value=None)
    seam.settings.github = SimpleNamespace(fallback_pat=None, app_id=None, app_private_key=None)
    seam.settings.mcp = SimpleNamespace(public_url=None, jwt_secret=None)
    for module in (dh, sa):
        monkeypatch.setattr(module, "identity_enabled_for", lambda *a: False)
    monkeypatch.setattr(dh, "load_agent_details", AsyncMock(return_value=details()))
    state = PanelState(
        roster=[],
        selected=None,
        account_id=ACCOUNT,
        guild_id=2001,
        is_admin=True,
        backend_options=("openai",),
    )
    actual = await dh.load_details_for(
        seam.runtime,
        state=state,
        agent=SimpleNamespace(
            name="analyst",
            ma_agent_id="ag_analyst",
        ),
    )
    assert actual.name == "analyst"
    assert state.backend_options == () and state.backend_selection is None
    monkeypatch.setattr(sa, "load_panel_roster", AsyncMock(return_value=object()))
    monkeypatch.setattr(sa, "load_panel_details", AsyncMock(return_value=details()))
    monkeypatch.setattr(sa, "resolve_attributions", AsyncMock(return_value={}))
    monkeypatch.setattr(sa, "coding_tools_available", lambda *a: False)
    modal = await read_slack_details(
        seam.runtime,
        tenant_id=TENANT,
        meta=PanelMetadata(team_id="T1", channel_id="C1", agent_name="analyst", view="details"),
        agent_name="analyst",
        is_admin=True,
    )
    assert modal is not None and "analyst" in str(modal)
    assert sv.ACTION_BACKEND_SELECT not in str(modal)
    panel = tp.SetupPanel(seam.runtime)
    panel._roster = AsyncMock(
        return_value=SimpleNamespace(
            rows=[
                SimpleNamespace(
                    name="analyst",
                    ma_agent_id="ag_analyst",
                )
            ]
        )
    )
    monkeypatch.setattr(tp, "load_agent_details", AsyncMock(return_value=details()))
    monkeypatch.setattr(tp, "load_grants_panel", AsyncMock(return_value=None))
    actor = Actor(user_id="admin", tenant_id=TENANT, is_admin=True, conversation_id="conversation")
    card = await panel._details(actor, "analyst", 0)
    assert card is not None and "analyst" in card.model_dump_json()
    assert "model_backend" not in card.model_dump_json()
    assert seam.calls == []


async def test_missing_sdk_source_is_absent_selection_with_no_write(persisted):
    trace = ScriptedTransport()
    trace.queue(ScriptedReply("GET", "/v1/agents", list_response([])))
    async with trace.client() as client:
        result = await persisted.service.read_selection(
            client, tenant_id=persisted.tenant.id, agent_name="missing"
        )
    assert result is None
    assert not trace.replies and not trace.violations
    async with persisted.sessions() as session:
        rows = await security_audit.list_events(session, tenant_id=persisted.tenant.id)
    assert [row.operation for row in rows] == ["allowlist"]


@pytest.mark.parametrize("typed", [True, False])
async def test_selection_read_swallows_only_typed_preview_refusal(persisted, typed):
    trace = ScriptedTransport()
    replies(trace, persisted.native, 1)
    error = (
        cb.AgentBackendRefused("preview_unavailable") if typed else RuntimeError("renderer broke")
    )

    def unavailable(*a):
        raise error

    service = cb.AgentBackendService(persisted.sessions, enabled=True, cost_line=unavailable)
    async with trace.client() as client:
        if typed:
            assert (
                await service.read_selection(
                    client, tenant_id=persisted.tenant.id, agent_name="analyst"
                )
                is None
            )
        else:
            with pytest.raises(RuntimeError) as caught:
                await service.read_selection(
                    client, tenant_id=persisted.tenant.id, agent_name="analyst"
                )
            assert caught.value is error
    assert not trace.replies and not trace.violations


async def test_disabled_selection_read_does_not_touch_client_or_database():
    def refuse(*a, **kw):
        raise AssertionError("disabled selection tried IO")

    service = cb.AgentBackendService(refuse, enabled=False)
    assert await service.read_selection(object(), tenant_id=TENANT, agent_name="analyst") is None
