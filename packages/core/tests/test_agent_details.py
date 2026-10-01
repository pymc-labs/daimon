"""Tests for the Details assembler: the pure fold and its one-retrieve shell.

The pure cases build an SDK agent with the shared builders and hand it rows
directly. The shell cases run the real SDK against a transport-level fake and
real Postgres, so the read count and the tenant guard are asserted on the
wire, not on a mock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.agent_details import (
    AgentDetails,
    GitHubDeploymentFacts,
    KeyEntry,
    build_agent_details,
    load_agent_details,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    ResolvedConfig,
)
from daimon.core.stores import scoped_config_write
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import AgentFileRow
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing import MARouter, build_fake_anthropic, ma_agent
from daimon.testing.factories import make_account, make_agent_repo_binding, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)
_NO_GITHUB = GitHubDeploymentFacts(has_fallback_pat=False, app_configured=False)
_PUBLIC_MCP_URL = "https://daimon.example/mcp"


def _details(
    agent: BetaManagedAgentsAgent,
    *,
    tenant_id: uuid.UUID,
    channels: list[ChannelConfigRow] | None = None,
    default: DeploymentDefault | None = None,
    resolved_here: ResolvedConfig | None = None,
    files: list[AgentFileRow] | None = None,
    public_mcp_url: str | None = _PUBLIC_MCP_URL,
    is_admin: bool = True,
    channel_label: str | None = "#general",
) -> AgentDetails:
    """Call the pure builder with the fields a case does not care about fixed."""
    return build_agent_details(
        agent=agent,
        tenant_id=tenant_id,
        tenant=None,
        channels=channels or [],
        default=default or DeploymentDefault(),
        resolved_here=resolved_here,
        binding=None,
        files=files or [],
        skill_titles={},
        skills_truncated=False,
        github=_NO_GITHUB,
        public_mcp_url=public_mcp_url,
        is_admin=is_admin,
        channel_label=channel_label,
    )


def test_key_entry_has_no_field_for_a_secret_value() -> None:
    """A key's value must have nowhere to go: this model is rendered into chat."""
    assert "content" not in KeyEntry.model_fields, (
        "KeyEntry must carry names and attribution only — a content field would put "
        "secret values one render away from a channel"
    )


def test_build_agent_details_sets_the_unrouted_note_when_the_agent_answers_nowhere() -> None:
    """An agent nobody can reach says so, in the reader's own voice."""
    tenant_id = uuid.uuid4()
    details = _details(ma_agent(name="alpha", tenant_id=tenant_id), tenant_id=tenant_id)
    assert details.answers_in == (), "no config row and no default means it answers nowhere"
    assert details.unrouted_note is not None, "an unreachable agent must carry the unrouted note"
    assert "alpha" in details.unrouted_note, "the note must name the agent it is about"
    assert "#general" in details.unrouted_note, (
        "with a channel in hand the note must name the concrete request to make"
    )


def test_build_agent_details_omits_the_unrouted_note_once_the_agent_answers_somewhere() -> None:
    """A routed agent needs no fixing, so it gets no note."""
    tenant_id = uuid.uuid4()
    channels = [
        ChannelConfigRow(tenant_id=tenant_id, channel_id="c1", agent_name="alpha", mode="agent")
    ]
    details = _details(
        ma_agent(name="alpha", tenant_id=tenant_id), tenant_id=tenant_id, channels=channels
    )
    assert details.answers_in == (AnsweringPlace(tier="channel", channel_id="c1"),), (
        "the channel row must be reported as the place it answers"
    )
    assert details.unrouted_note is None, "a routed agent must not be told it is unrouted"


def test_build_agent_details_omits_the_deployments_own_mcp_server() -> None:
    """Every agent carries daimon's own server; only what the reader connected is listed."""
    tenant_id = uuid.uuid4()
    agent = ma_agent(
        name="alpha",
        tenant_id=tenant_id,
        mcp_servers=[
            {"type": "url", "name": "daimon", "url": _PUBLIC_MCP_URL},
            {"type": "url", "name": "linear", "url": "https://linear.example/mcp"},
        ],
    )
    details = _details(agent, tenant_id=tenant_id)
    assert [server.name for server in details.mcp_servers] == ["linear"], (
        "the deployment's own MCP server is how the product works, not a connection "
        "the reader made, and listing it invites removing it"
    )


def test_build_agent_details_keeps_every_server_when_the_deployment_has_no_public_url() -> None:
    """With no public URL configured there is nothing to match, so nothing is dropped."""
    tenant_id = uuid.uuid4()
    agent = ma_agent(
        name="alpha",
        tenant_id=tenant_id,
        mcp_servers=[{"type": "url", "name": "linear", "url": "https://linear.example/mcp"}],
    )
    details = _details(agent, tenant_id=tenant_id, public_mcp_url=None)
    assert [server.name for server in details.mcp_servers] == ["linear"], (
        "an unconfigured public URL must not filter out a real external connection"
    )


def test_build_agent_details_marks_an_agent_stamped_with_the_workspace_account() -> None:
    """A seeded or panel-created agent belongs to the install, not to a person."""
    tenant_id = uuid.uuid4()
    workspace_account = derive_guild_account_uuid(tenant_id)
    agent = ma_agent(
        name="alpha",
        tenant_id=tenant_id,
        metadata={MA_METADATA_KEY_ACCOUNT: str(workspace_account)},
    )
    details = _details(agent, tenant_id=tenant_id)
    assert details.created_by_account_id == workspace_account, "the stamped account is reported"
    assert details.created_by_is_workspace, (
        "an agent stamped with the install's own account must not be attributed to a person"
    )


def test_build_agent_details_attributes_an_agent_stamped_with_a_persons_account() -> None:
    """Anyone else's account id is a person, and is reported as one."""
    tenant_id = uuid.uuid4()
    creator = uuid.uuid4()
    agent = ma_agent(
        name="alpha", tenant_id=tenant_id, metadata={MA_METADATA_KEY_ACCOUNT: str(creator)}
    )
    details = _details(agent, tenant_id=tenant_id)
    assert details.created_by_account_id == creator, "the creator's account id is reported"
    assert not details.created_by_is_workspace, (
        "only the install's own derived account counts as workspace-created"
    )


def test_build_agent_details_treats_an_unparseable_account_stamp_as_no_creator() -> None:
    """MA metadata is free-form across codebase generations, so a junk stamp reads as absent."""
    tenant_id = uuid.uuid4()
    agent = ma_agent(
        name="alpha", tenant_id=tenant_id, metadata={MA_METADATA_KEY_ACCOUNT: "not-a-uuid"}
    )
    details = _details(agent, tenant_id=tenant_id)
    assert details.created_by_account_id is None, (
        "a stamp that is not a uuid means the same thing as no stamp at all, not a crash"
    )
    assert not details.created_by_is_workspace, (
        "an unreadable stamp must not be mistaken for the install's own account"
    )


def test_build_agent_details_names_the_model_and_when_changes_take_effect() -> None:
    """The model gets its product name, and the availability sentence is fixed."""
    tenant_id = uuid.uuid4()
    details = _details(
        ma_agent(name="alpha", model="claude-sonnet-4-6", tenant_id=tenant_id), tenant_id=tenant_id
    )
    assert details.model_id == "claude-sonnet-4-6", "the raw model id stays available"
    assert details.model_display_name == "Sonnet 4.6", "the reader sees the product name"
    assert details.applies_note == "Changes to alpha apply from the next message to it.", (
        "the availability sentence names the agent and makes no per-change claim"
    )


def test_build_agent_details_reports_keys_by_name_and_attribution_only() -> None:
    """A key's presence, who added it and who last set it — never its value."""
    tenant_id = uuid.uuid4()
    creator = uuid.uuid4()
    setter = uuid.uuid4()
    files = [
        AgentFileRow(
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            key="LINEAR_API_KEY",
            content="super-secret",
            created_by_account_id=creator,
            last_set_by_account_id=setter,
            created_at=_NOW,
            updated_at=_NOW,
        )
    ]
    details = _details(
        ma_agent(name="alpha", tenant_id=tenant_id), tenant_id=tenant_id, files=files
    )
    assert [key.name for key in details.keys] == ["LINEAR_API_KEY"], "the key is listed by name"
    assert details.keys[0].created_by_account_id == creator, "the key carries who added it"
    assert details.keys[0].last_set_by_account_id == setter, "the key carries who last set it"
    assert "super-secret" not in details.model_dump_json(), (
        "no rendering of AgentDetails may ever contain a key's value"
    )


def test_build_agent_details_answers_here_follows_the_resolved_responder() -> None:
    """Whether this agent answers where the reader stands is the cascade's answer, not a guess."""
    tenant_id = uuid.uuid4()
    agent = ma_agent(name="alpha", tenant_id=tenant_id)
    answering = _details(
        agent, tenant_id=tenant_id, resolved_here=ResolvedConfig(agent_name="alpha")
    )
    not_answering = _details(
        agent, tenant_id=tenant_id, resolved_here=ResolvedConfig(agent_name="beta")
    )
    assert answering.answers_here, "the resolved responder is this agent"
    assert not not_answering.answers_here, "another agent answers where the reader stands"


async def test_load_agent_details_reads_the_agent_once_and_wires_its_stored_rows(
    db_session: AsyncSession,
) -> None:
    """One MA retrieve, and the DB rows keyed by the derived agent id come back with it."""
    tenant = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=tenant)
    agent = ma_agent(id="ag_alpha", name="alpha", tenant_id=tenant.id)
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_alpha")
    await make_agent_repo_binding(
        db_session, tenant=tenant, agent_id=agent_uuid, repo_url="acme/widgets"
    )
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="LINEAR_API_KEY",
        content="super-secret",
        set_by_account_id=actor.id,
    )
    router = MARouter()
    router.add_agent(agent)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return router.dispatch(request)

    details = await load_agent_details(
        db_session,
        build_fake_anthropic(handler),
        tenant_id=tenant.id,
        ma_agent_id="ag_alpha",
        platform="discord",
        channel_id="c1",
        thread_id=None,
        deployment_default=DeploymentDefault(agent_name="daimon"),
        github=_NO_GITHUB,
        public_mcp_url=_PUBLIC_MCP_URL,
        is_admin=True,
        channel_label="#general",
    )

    assert seen == ["/v1/agents/ag_alpha"], (
        "the whole Details read must cost exactly one agent retrieve and no skills listing"
    )
    assert details.repo is not None and details.repo.repo_url == "acme/widgets", (
        "the binding stored under the derived agent id must be found"
    )
    assert [key.name for key in details.keys] == ["LINEAR_API_KEY"], (
        "the agent's stored keys must be listed by name"
    )
    assert details.repo.access.kind == "needs_attention", (
        "a binding with no token, no proof and no deployment credential needs attention"
    )


async def test_load_agent_details_lists_skills_only_when_a_custom_skill_is_attached(
    db_session: AsyncSession,
) -> None:
    """A custom skill has an opaque id, so its display title costs a second call."""
    tenant = await make_tenant(db_session)
    agent = ma_agent(
        id="ag_alpha",
        name="alpha",
        tenant_id=tenant.id,
        skills=[{"type": "custom", "skill_id": "skill_1", "version": "1"}],
    )
    router = MARouter()
    router.add_agent(agent)
    router.add(
        "GET",
        r"/v1/skills",
        lambda _request, _match: httpx.Response(
            status_code=200,
            json={
                "data": [
                    {
                        "id": "skill_1",
                        "type": "skill",
                        "created_at": "2026-01-01T00:00:00Z",
                        "display_title": f"{str(tenant.id)[:8]}-forecasting",
                        "latest_version": "1",
                    }
                ],
                "has_more": False,
            },
        ),
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return router.dispatch(request)

    details = await load_agent_details(
        db_session,
        build_fake_anthropic(handler),
        tenant_id=tenant.id,
        ma_agent_id="ag_alpha",
        platform="discord",
        channel_id=None,
        thread_id=None,
        deployment_default=DeploymentDefault(),
        github=_NO_GITHUB,
        public_mcp_url=None,
        is_admin=True,
        channel_label=None,
    )

    assert seen == ["/v1/agents/ag_alpha", "/v1/skills"], (
        "an attached custom skill is what buys the skills listing"
    )
    assert [skill.title for skill in details.skills] == ["forecasting"], (
        "the custom skill must be named by its tenant-stripped display title"
    )


async def test_load_agent_details_reports_answers_here_from_a_bound_threads_responder(
    db_session: AsyncSession,
) -> None:
    """Inside a bound thread the question is who answers in the thread, not the channel."""
    tenant = await make_tenant(db_session)
    agent = ma_agent(id="ag_alpha", name="alpha", tenant_id=tenant.id)
    await scoped_config_write.set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="beta",
        actor_account_id=None,
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c1",
        thread_id="t1",
        responder_ma_agent_id="ag_alpha",
        responder_name="alpha",
        kind="handoff",
    )
    router = MARouter()
    router.add_agent(agent)

    in_thread = await load_agent_details(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        ma_agent_id="ag_alpha",
        platform="discord",
        channel_id="c1",
        thread_id="t1",
        deployment_default=DeploymentDefault(),
        github=_NO_GITHUB,
        public_mcp_url=None,
        is_admin=True,
        channel_label="#general",
    )
    in_channel = await load_agent_details(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        ma_agent_id="ag_alpha",
        platform="discord",
        channel_id="c1",
        thread_id=None,
        deployment_default=DeploymentDefault(),
        github=_NO_GITHUB,
        public_mcp_url=None,
        is_admin=True,
        channel_label="#general",
    )

    assert in_thread.answers_here, "the thread's own responder is this agent"
    assert not in_channel.answers_here, "the parent channel is routed to another agent"


async def test_load_agent_details_refuses_an_agent_from_another_install(
    db_session: AsyncSession,
) -> None:
    """The tenant guard runs before anything is read, so no cross-install detail leaks."""
    tenant = await make_tenant(db_session, workspace_id="guild-1")
    other = await make_tenant(db_session, workspace_id="guild-2")
    router = MARouter()
    router.add_agent(ma_agent(id="ag_theirs", name="theirs", tenant_id=other.id))

    with pytest.raises(DaimonError, match="no longer available"):
        await load_agent_details(
            db_session,
            build_fake_anthropic(router.dispatch),
            tenant_id=tenant.id,
            ma_agent_id="ag_theirs",
            platform="discord",
            channel_id="c1",
            thread_id=None,
            deployment_default=DeploymentDefault(),
            github=_NO_GITHUB,
            public_mcp_url=None,
            is_admin=True,
            channel_label="#general",
        )
