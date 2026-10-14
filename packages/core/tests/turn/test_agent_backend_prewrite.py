"""The awaited proof precedes publication in the existing revision namespace."""

import asyncio

import pytest
from daimon.core.channel_backend import resolve_turn_backend
from daimon.core.config import TurnSettings
from daimon.core.stores import mux_state
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.ids import ChannelRef
from sqlalchemy import text

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned


def test_native_agent_execution_defaults_off():
    assert TurnSettings().agent_backends is False
    assert TurnSettings(path="mux", channel_backends=True).agent_backends is False


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("failure", ["static", "proof", "cancelled"])
async def test_refusal_precedes_insert_and_cannot_be_read_by_old_channel_reader(
    owned, provider, failure
):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, provider, 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="slack", channel_id="proof")
    checks = []

    async def verify(selection, origin, candidate):
        checks.append(candidate)
        assert origin == "agent"
        assert selection.backend == provider
        if candidate is None and failure != "static":
            return
        if failure == "cancelled":
            raise asyncio.CancelledError
        raise AdmissionDenied(reason="backend_unsupported")

    async with sessions.begin() as session:
        with pytest.raises(asyncio.CancelledError if failure == "cancelled" else AdmissionDenied):
            await resolve_turn_backend(
                session, channel, agent, enabled=True, validate_origin=verify
            )
        assert await mux_state.latest_config_revision(session, channel) is None
        # A pre-origin-schema image reads all rows in this namespace as overrides.
        count = await session.scalar(
            text(
                "SELECT count(*) FROM channel_config_revision WHERE tenant_id=:tenant AND channel_id=:channel"
            ),
            {"tenant": agent.tenant_id, "channel": channel.channel_id},
        )
        assert count == 0
    assert checks == [None] if failure == "static" else len(checks) == 2


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_exact_candidate_is_verified_before_write_and_existing_revision_rechecks(
    owned, provider
):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, provider, 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="slack", channel_id="verified")
    checks = []

    async def verify(selection, origin, candidate):
        assert origin == "agent"
        if candidate is not None:
            checks.append(candidate)
            assert candidate.channel == channel and candidate.backend == provider
        await asyncio.sleep(0)

    async with sessions.begin() as session:
        revision, _, source = await resolve_turn_backend(
            session, channel, agent, enabled=True, validate_origin=verify
        )
        assert source == "agent" and revision is checks[0]
    async with sessions.begin() as session:
        repeated, _, _ = await resolve_turn_backend(
            session, channel, agent, enabled=True, validate_origin=verify
        )
        assert repeated == revision == checks[-1]
    assert len(checks) == 2


def canonical_worker(agent, provider, source=None):
    from dataclasses import replace

    from mux.contracts.ids import ThreadRef

    from ..test_fleet_permissions import parent, resolve

    root = parent(provider)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="slack", channel_id="worker")
    root = replace(
        root,
        agent_id=agent.agent_id,
        fleet=replace(
            root.fleet,
            scope=root.fleet.scope.model_copy(update={"tenant_id": channel.tenant_id}),
            root=ThreadRef(channel=channel, thread_id="thread"),
            place=replace(root.fleet.place, parent_channel_id=channel.channel_id),
        ),
    )
    if source is not None:
        from uuid import UUID

        root = replace(
            root,
            fleet=replace(
                root.fleet,
                account_id=UUID(source.ref.account_id),
                scope=root.fleet.scope.model_copy(update={"account_id": source.ref.account_id}),
            ),
            source_agents=(source.ref,),
            source_revisions=((source.ref, source.revision),),
        )
    return resolve(root, source), channel


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_worker_selection_precedes_channel_preferences_and_rechecks_current_policy(
    owned, provider
):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    worker, channel = canonical_worker(agent, provider)
    from daimon.core.channel_backend import (
        AgentBackendRefused,
        current_backend,
        set_channel_backend,
    )
    from mux.contracts.config import BackendConfig

    seen = []

    async def verify(selection, origin, candidate):
        assert origin == "agent" and selection == worker.backend
        seen.append(candidate)

    async with sessions.begin() as session:
        configured = await set_channel_backend(session, channel, BackendConfig(backend="anthropic"))
        revision, _, origin = await resolve_turn_backend(
            session, channel, agent, enabled=True, worker=worker, validate_origin=verify
        )
        assert origin == "agent" and revision is not None
        assert revision.content_digest() == worker.backend.content_digest()
        assert await current_backend(session, channel) == configured
    async with sessions.begin() as session:
        repeated, _, _ = await resolve_turn_backend(
            session,
            channel,
            agent,
            enabled=True,
            worker=worker,
            retained_revision=revision.local,
            validate_origin=verify,
        )
        assert repeated == revision
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(agent.tenant_id, ("anthropic",), policy.revision, actor)
    async with sessions.begin() as session:
        if provider != "anthropic":
            with pytest.raises(AgentBackendRefused, match="backend not allowed"):
                await resolve_turn_backend(
                    session, channel, agent, enabled=True, worker=worker, validate_origin=verify
                )
        else:
            assert (
                await resolve_turn_backend(
                    session, channel, agent, enabled=True, worker=worker, validate_origin=verify
                )
            )[0] == revision
    assert seen == [None, revision] * (3 if provider == "anthropic" else 2)


@pytest.mark.parametrize(
    "replacement", ["validator", "tenant", "account", "agent", "spec", "model", "clone"]
)
async def test_worker_identity_refuses_before_effective_revision_insert(owned, replacement):
    from dataclasses import replace
    from uuid import uuid4

    from daimon.core.channel_backend import AgentBackendRefused
    from mux.contracts.ids import ResourceRef, Revision

    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    worker, channel = canonical_worker(agent, "openai")

    async def verify(*args):
        pass

    validator = verify
    if replacement == "validator":
        validator = None
    elif replacement in ("tenant", "account"):
        fleet = worker.permissions.fleet
        scope = fleet.scope.model_copy(update={replacement + "_id": str(uuid4())})
        worker = replace(
            worker, permissions=replace(worker.permissions, fleet=replace(fleet, scope=scope))
        )
    elif replacement == "agent":
        worker = replace(worker, permissions=replace(worker.permissions, agent_id=uuid4()))
    elif replacement == "clone":
        worker = replace(
            worker,
            source_agent=(
                ResourceRef(
                    id="foreign",
                    kind="agent",
                    provider="openai",
                    account_scope_id="project",
                    tenant_id=channel.tenant_id,
                ),
                Revision(local=1),
            ),
        )
    else:
        changed = (
            worker.spec.model_copy(update={"system": "changed"})
            if replacement == "spec"
            else worker.spec.model_copy(
                update={"model": worker.spec.model.model_copy(update={"id": "ungranted"})}
            )
        )
        worker = replace(worker, spec=changed)
    async with sessions.begin() as session:
        with pytest.raises(AgentBackendRefused):
            await resolve_turn_backend(
                session, channel, agent, enabled=True, worker=worker, validate_origin=validator
            )
        assert await mux_state.latest_config_revision(session, channel) is None


async def test_worker_source_refusal_and_cancel_precede_allocation_revision(owned):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    worker, channel = canonical_worker(agent, "openai")

    async def refuse(*args):
        raise AdmissionDenied(reason="backend_unsupported")

    async with sessions.begin() as session:
        with pytest.raises(AdmissionDenied):
            await resolve_turn_backend(
                session, channel, agent, enabled=True, worker=worker, validate_origin=refuse
            )
        assert await mux_state.latest_config_revision(session, channel) is None


async def test_clone_uses_actual_source_identity_and_keeps_distinct_root_authority(owned):
    from datetime import UTC, datetime
    from uuid import uuid4

    from mux.contracts.ids import ResourceRef, Revision
    from mux.contracts.resources import Agent

    service, root_agent, actor, sessions = owned
    await enable(service, root_agent, actor)
    fresh, channel = canonical_worker(root_agent, "openai")
    source = Agent(
        ref=ResourceRef(
            id="actual-clone-source",
            kind="agent",
            provider="openai",
            account_scope_id="project",
            tenant_id=channel.tenant_id,
            account_id=fresh.permissions.fleet.scope.account_id,
        ),
        revision=Revision(local=3),
        created_at=datetime.now(UTC),
        spec=fresh.spec,
    )
    worker, channel = canonical_worker(root_agent, "openai", source)
    actual_source = root_agent.model_copy(update={"agent_id": uuid4(), "source": source})
    assert worker.permissions.agent_id != actual_source.agent_id

    async def verify(selection, origin, candidate):
        assert selection == worker.backend and origin == "agent"

    async with sessions.begin() as session:
        revision, _, origin = await resolve_turn_backend(
            session, channel, actual_source, enabled=True, worker=worker, validate_origin=verify
        )
        assert revision is not None and origin == "agent"
        assert worker.source_agent == (source.ref, source.revision)
