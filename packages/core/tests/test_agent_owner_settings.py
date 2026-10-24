"""An agent carries its own settings, and its owner may change them (PR C)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    AgentSettings,
    BackendActor,
    BackendAgent,
    BackendUnsupported,
    agent_selection,
    resolve_turn_backend,
    revision_selection,
    set_channel_backend,
)
from daimon.core.stores import agent_owners, mux_state
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.config import BackendConfig, CapabilityRequirement
from mux.contracts.ids import ChannelRef
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

REQUIRES_MULTIAGENT = {"multiagent": CapabilityRequirement(level="required")}
REQUIRES_TOOLS = {"tool_loop": CapabilityRequirement(level="required")}


@pytest.fixture
async def people(db_preflight: DatabasePreflight, db_committing_engine: AsyncEngine):
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        admin = await make_account(session, tenant=tenant)
        owner = await make_account(session, tenant=tenant)
        member = await make_account(session, tenant=tenant)
        other_tenant = await make_tenant(session)
        stranger = await make_account(session, tenant=other_tenant)
    agent = BackendAgent(
        tenant_id=tenant.id, agent_id=uuid.uuid4(), name="helper", configuration_digest="c"
    )
    service = AgentBackendService(sessions, enabled=True)
    actors = {
        "admin": BackendActor(tenant.id, admin.id, Subject(is_admin=True)),
        "owner": BackendActor(tenant.id, owner.id, Subject(is_admin=False)),
        "member": BackendActor(tenant.id, member.id, Subject(is_admin=False)),
    }
    async with sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=tenant.id,
            agent_id=agent.agent_id,
            owner_account_id=owner.id,
            source="admin_assigned",
        )
        with pytest.raises(LookupError):
            await agent_owners.set_owner(
                session,
                tenant_id=tenant.id,
                agent_id=uuid.uuid4(),
                owner_account_id=stranger.id,
                source="admin_assigned",
            )
    policy = await service.tenant_policy(tenant.id)
    await service.set_tenant_allowlist(
        tenant.id, ("anthropic", "openai", "gemini"), policy.revision, actors["admin"]
    )
    return service, agent, actors, sessions


def channel_of(agent: BackendAgent, name: str = "chan") -> ChannelRef:
    return ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id=name)


def test_unset_settings_keep_the_backend_selection_and_its_digest() -> None:
    assert agent_selection("openai", None, AgentSettings()) == agent_selection("openai")
    assert AgentSettings().facts() == {}
    shared = agent_selection("anthropic", None, AgentSettings(thread_mode="shared"))
    assert shared.thread_mode == "shared" and shared.profile == "anthropic.managed_agents"
    with pytest.raises(AgentBackendRefused, match="profile not selectable"):
        agent_selection("openai", None, AgentSettings(profile="gemini.inline_reuse"))
    with pytest.raises(AgentBackendRefused, match="profile not selectable"):
        agent_selection("openai", None, AgentSettings(profile="openai.nonexistent"))
    conversation = agent_selection(
        "openai", None, AgentSettings(profile="openai.conversation_only")
    )
    assert conversation.profile == "openai.conversation_only"


def test_a_profile_is_dropped_only_when_the_provider_changes() -> None:
    settings = AgentSettings("openai.conversation_only", REQUIRES_MULTIAGENT, "shared")
    assert settings.for_backend("openai") == settings
    moved = settings.for_backend("gemini")
    assert moved.profile is None and moved.thread_mode == "shared" and moved.requires


async def test_the_owner_edits_settings_backend_and_fleet_and_a_member_cannot(people) -> None:
    service, agent, actors, _ = people
    settings = AgentSettings(thread_mode="shared")
    preview = await service.preview(agent, None, settings=settings)
    assert preview.target_settings == {"thread_mode": "shared"}
    with pytest.raises(AgentBackendRefused, match="owner or admin required"):
        await service.set_settings(
            agent, settings, 0, actors["member"], expected_preview_digest=preview.digest
        )
    assert await service.current(agent) is None
    # Sharing threads stays an admin's call: the owner is refused, an admin is not.
    with pytest.raises(AgentBackendRefused, match="admin required for shared threads"):
        await service.set_settings(
            agent, settings, 0, actors["owner"], expected_preview_digest=preview.digest
        )
    assert await service.current(agent) is None
    revision = await service.set_settings(
        agent, settings, 0, actors["admin"], expected_preview_digest=preview.digest
    )
    assert revision.thread_mode == "shared" and revision.backend is None
    assert revision_selection(revision) == agent_selection("anthropic", None, settings)

    # Shared threads don't run on OpenAI: the switch is refused, never silently per caller.
    with pytest.raises(BackendUnsupported):
        await service.set_backend(agent, "openai", 1, actors["owner"])
    await service.set_settings(agent, AgentSettings(requires=REQUIRES_TOOLS), 1, actors["owner"])
    switched = await service.set_backend(agent, "openai", 2, actors["owner"])
    assert switched.backend == "openai" and switched.requires == REQUIRES_TOOLS
    reverted = await service.revert(agent, actors["owner"], expected_revision=3)
    assert reverted.backend is None and reverted.requires == REQUIRES_TOOLS

    now = datetime.now(UTC)
    grant_preview = await service.preview_fleet_grant(agent, None, now=now)
    with pytest.raises(AgentBackendRefused, match="owner or admin required"):
        await service.revoke_fleet_grant(
            agent, 4, actors["member"], expected_preview_digest=grant_preview.digest, now=now
        )
    with pytest.raises(AgentBackendRefused, match="admin required"):
        await service.revoke_fleet_grant(
            agent, 4, actors["owner"], expected_preview_digest=grant_preview.digest, now=now
        )
    revoked = await service.revoke_fleet_grant(
        agent, 4, actors["admin"], expected_preview_digest=grant_preview.digest, now=now
    )
    assert revoked.requires == REQUIRES_TOOLS
    rows = await service.audit(agent.tenant_id, agent_id=agent.agent_id)
    assert {row.operation for row in rows} >= {"settings", "set", "revert", "fleet_grant_revoke"}
    assert {row.account_id for row in rows if row.operation == "settings"} == {
        actors["admin"].account_id,
        actors["owner"].account_id,
    }


async def test_an_owner_can_not_make_or_restore_shared_threads(people) -> None:
    service, agent, actors, _ = people
    await service.set_settings(agent, AgentSettings(thread_mode="shared"), 0, actors["admin"])
    # The owner may keep an admin's sharing while changing other settings...
    kept = await service.set_settings(
        agent, AgentSettings(requires=REQUIRES_TOOLS, thread_mode="shared"), 1, actors["owner"]
    )
    assert kept.thread_mode == "shared"
    # ...and may turn it off, but not back on, by setting or by revert.
    await service.set_settings(agent, AgentSettings(thread_mode="per_caller"), 2, actors["owner"])
    with pytest.raises(AgentBackendRefused, match="admin required for shared threads"):
        await service.revert(agent, actors["owner"], expected_revision=3)
    with pytest.raises(AgentBackendRefused, match="admin required for shared threads"):
        await service.set_settings(agent, AgentSettings(thread_mode="shared"), 3, actors["owner"])
    assert (await service.current(agent)).thread_mode == "per_caller"
    restored = await service.revert(agent, actors["admin"], expected_revision=3)
    assert restored.thread_mode == "shared"


async def test_the_owner_can_not_change_tenant_settings(people) -> None:
    service, agent, actors, _ = people
    policy = await service.tenant_policy(agent.tenant_id)
    with pytest.raises(AgentBackendRefused, match="admin required"):
        await service.set_tenant_allowlist(
            agent.tenant_id, ("anthropic",), policy.revision, actors["owner"]
        )


@pytest.mark.parametrize("trust", ["via_agent_key", "client"])
async def test_an_agent_key_or_a_client_is_never_the_owner(people, trust) -> None:
    service, agent, actors, _ = people
    owner = actors["owner"]
    untrusted = BackendActor(owner.tenant_id, owner.account_id, Subject(**{trust: True}))
    assert not await service.may_edit(agent, untrusted)
    with pytest.raises(AgentBackendRefused, match="owner or admin required"):
        await service.set_backend(agent, "openai", 0, untrusted)


async def test_ownership_is_read_at_the_write_not_at_the_preview(people) -> None:
    service, agent, actors, sessions = people
    preview = await service.preview(agent, "openai")
    assert await service.may_edit(agent, actors["owner"])
    async with sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=actors["member"].account_id,
            source="admin_assigned",
        )
    assert not await service.may_edit(agent, actors["owner"])
    with pytest.raises(AgentBackendRefused, match="owner or admin required"):
        await service.set_backend(
            agent, "openai", 0, actors["owner"], expected_preview_digest=preview.digest
        )
    async with sessions.begin() as session:
        assert await agent_owners.clear_owner(
            session, tenant_id=agent.tenant_id, agent_id=agent.agent_id
        )
        assert await agent_owners.owner_of(session, agent.tenant_id, agent.agent_id) is None
    assert not await service.may_edit(agent, actors["member"])
    assert await service.may_edit(agent, actors["admin"])


async def test_record_creator_never_replaces_an_owner(people) -> None:
    _, agent, actors, sessions = people
    async with sessions.begin() as session:
        assert not await agent_owners.record_creator(
            session,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            account_id=actors["member"].account_id,
        )
        assert (
            await agent_owners.owner_of(session, agent.tenant_id, agent.agent_id)
            == actors["owner"].account_id
        )
        fresh = uuid.uuid4()
        assert await agent_owners.record_creator(
            session,
            tenant_id=agent.tenant_id,
            agent_id=fresh,
            account_id=actors["member"].account_id,
        )


async def test_the_agents_shared_intent_admits_without_a_channel_pin(people) -> None:
    service, agent, actors, sessions = people
    await service.set_settings(agent, AgentSettings(thread_mode="shared"), 0, actors["admin"])
    channel = channel_of(agent)
    async with sessions.begin() as session:
        revision, _, source = await resolve_turn_backend(session, channel, agent, enabled=True)
    assert source == "agent" and revision is not None and revision.thread_mode == "shared"
    # Back to per caller: the channel still knows it once shared threads.
    await service.set_settings(agent, AgentSettings(), 1, actors["owner"])
    async with sessions.begin() as session:
        latest, ever_shared = await mux_state.latest_config_revision_and_sharing(
            session, channel, overrides_only=True
        )
    assert latest is None and ever_shared


async def test_a_channel_pin_tightens_but_never_drops_a_required_capability(people) -> None:
    service, agent, actors, sessions = people
    await service.set_settings(
        agent,
        AgentSettings(requires=REQUIRES_MULTIAGENT, thread_mode="shared"),
        0,
        actors["admin"],
    )
    per_caller = channel_of(agent, "per-caller")
    lacking = channel_of(agent, "lacking")
    async with sessions.begin() as session:
        pin = await set_channel_backend(session, per_caller, BackendConfig())
        await set_channel_backend(
            session, lacking, BackendConfig(backend="openai", model="gpt-6-luna")
        )
    async with sessions.begin() as session:
        revision, _, source = await resolve_turn_backend(session, per_caller, agent, enabled=True)
    assert source == "channel" and revision == pin and revision.thread_mode == "per_caller"
    async with sessions.begin() as session:
        with pytest.raises(AgentBackendRefused, match="channel pin lacks capability"):
            await resolve_turn_backend(session, lacking, agent, enabled=True)


async def test_an_agent_requirement_its_backend_lacks_is_refused_at_the_write(people) -> None:
    service, agent, actors, _ = people
    await service.set_backend(agent, "openai", 0, actors["owner"])
    preview = await service.preview_settings(agent, AgentSettings(requires=REQUIRES_MULTIAGENT))
    assert "multiagent" in preview.unsupported_capabilities
    with pytest.raises(Exception, match="multiagent"):
        await service.set_settings(
            agent, AgentSettings(requires=REQUIRES_MULTIAGENT), 1, actors["owner"]
        )
    assert (await service.current(agent)).requires is None


async def test_old_revisions_keep_their_digest(people) -> None:
    service, agent, actors, _ = people
    revision = await service.set_backend(agent, "openai", 0, actors["admin"])
    from daimon.core.channel_backend import _revision_digest

    assert revision.digest == _revision_digest(agent, "openai", None, None, None)
