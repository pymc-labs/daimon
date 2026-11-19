"""A switch's losses ride the policy, revision and audit transaction; none is asked for."""

import asyncio

import pytest
from daimon.core.channel_backend import AgentBackendRefused, AgentBackendService
from daimon.core.stores import security_audit

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned
from .source_projection_support import captured_source


@pytest.fixture
async def switching(owned):
    _, agent, actor, sessions = owned
    source = captured_source()
    source = source.model_copy(
        update={
            "tenant_id": agent.tenant_id,
            "agent_id": agent.agent_id,
            "effective_spec": source.effective_spec.model_copy(
                update={"tools": (), "skills": (), "mcp_servers": ()}
            ),
            "asks_before_publishing": False,
        }
    )
    captured, reads = [source], []

    async def capture(actual):
        reads.append(actual)
        return captured[0]

    service = AgentBackendService(sessions, enabled=True, capture_source=capture)
    await enable(service, agent, actor)
    return service, agent, actor, sessions, captured, reads


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_a_switch_writes_and_audits_what_the_source_loses(switching, provider):
    """No acceptance is asked: the write reads the source and audits what is off."""
    service, agent, actor, sessions, _, reads = switching
    preview = await service.preview(agent, provider)
    assert preview.allowed and preview.declared_drops == ("sandbox",), preview
    revision = await service.set_backend(agent, provider, 0, actor)
    assert reads == [agent, agent], "the preview and the write each read the source"
    assert revision.preview_digest == preview.digest, "the write's preview is the one shown"
    assert await service.revision(agent, 1) == revision, "the revision reads back"
    async with sessions() as db:
        audit = await security_audit.list_events(db, tenant_id=agent.tenant_id)
    assert [row.operation for row in audit] == ["allowlist", "set"], audit
    assert audit[-1].reason == "completed; declared_drops=sandbox", audit[-1].reason
    assert audit[-1].account_id == actor.account_id, "audited as the person who switched"


async def test_a_source_edited_after_the_preview_switches_with_the_edit(switching):
    """The write reads the source again: a loss added since the preview is audited."""
    service, agent, actor, _, captured, _ = switching
    preview = await service.preview(agent, "openai")
    assert preview.declared_drops == ("sandbox",), preview
    captured[0] = captured[0].model_copy(update={"memory_required": True})
    revision = await service.set_backend(agent, "openai", 0, actor)
    assert revision.backend == "openai", "the switch applies over the edit"
    audit = await service.audit(agent.tenant_id)
    assert audit[-1].reason == "completed; declared_drops=memory,sandbox", audit[-1].reason


async def test_concurrent_switches_have_one_revision_and_audit(switching):
    """Two switches from one revision: one writes, the other is refused as stale."""
    service, agent, actor, _, _, _ = switching

    async def change():
        return await service.set_backend(agent, "openai", 0, actor)

    results = await asyncio.gather(change(), change(), return_exceptions=True)
    assert sum(isinstance(value, AgentBackendRefused) for value in results) == 1, results
    assert (await service.current(agent)).local == 1, "one revision"
    audit = await service.audit(agent.tenant_id)
    assert [row.operation for row in audit] == ["allowlist", "set"], "one audit row"


async def test_clear_and_revert_apply_over_any_source_change(switching):
    """Back to Claude and back again: each applies, whatever the source gained meanwhile."""
    service, agent, actor, _, captured, reads = switching
    await service.set_backend(agent, "openai", 0, actor)
    cleared = await service.clear_backend(agent, 1, actor)
    assert cleared.backend is None, "cleared to Claude"
    restored = await service.revert(agent, actor, expected_revision=2)
    assert restored.backend == "openai", "the revert restores OpenAI"
    assert len(reads) == 2, "clear reads no source; set and revert each read it once"
    await service.clear_backend(agent, 3, actor)
    captured[0] = captured[0].model_copy(update={"memory_required": True})
    again = await service.revert(agent, actor, expected_revision=4)
    assert (again.local, again.backend) == (5, "openai"), "a new loss needs no acceptance"
    audit = await service.audit(agent.tenant_id)
    assert audit[-1].reason == "completed; declared_drops=memory,sandbox", audit[-1].reason


@pytest.mark.parametrize("publishing", [None, True])
async def test_unknown_or_required_publishing_approval_is_never_dropped(switching, publishing):
    service, agent, actor, _, captured, _ = switching
    captured[0] = captured[0].model_copy(update={"asks_before_publishing": publishing})
    with pytest.raises(AgentBackendRefused, match="source unsupported"):
        await service.preview(agent, "openai")
    assert await service.current(agent) is None
    assert [row.operation for row in await service.audit(agent.tenant_id)] == ["allowlist"]


async def test_a_read_only_service_never_writes(switching):
    """A service built only to read refuses every change, a revert included."""
    service, agent, actor, sessions, _, _ = switching
    await service.set_backend(agent, "openai", 0, actor)
    await service.clear_backend(agent, 1, actor)
    reader = AgentBackendService(sessions, enabled=True, read_only=True)
    before = await service.audit(agent.tenant_id)
    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await reader.revert(agent, actor, expected_revision=2)
    assert (await service.current(agent)).local == 2, "nothing written"
    assert await service.audit(agent.tenant_id) == before, "nothing audited"


async def test_a_switch_without_a_source_capture_declares_nothing(owned):
    """With no source to read, nothing is declared off; the run decides what it carries."""
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    preview = await service.preview(agent, "openai")
    assert not preview.declared_drops, preview
    revision = await service.set_backend(agent, "openai", 0, actor)
    assert revision.backend == "openai", "written"
    assert (await service.audit(agent.tenant_id))[-1].reason == "completed", "no loss audited"


async def test_a_revert_to_a_switch_made_before_captures_applies(switching):
    """An older revision written without a source capture reverts like any other."""
    service, agent, actor, sessions, _, reads = switching
    legacy = AgentBackendService(sessions, enabled=True)
    original = await legacy.set_backend(agent, "openai", 0, actor)
    await legacy.clear_backend(agent, original.local, actor)
    restored = await service.revert(agent, actor, expected_revision=2)
    assert len(reads) == 1, "the revert reads the source once"
    assert (restored.local, restored.backend) == (3, "openai"), "reverted"


async def test_the_source_is_captured_before_the_policy_lock(switching, monkeypatch):
    """A capture may read every skill: never while other edits wait on the tenant's lock."""
    from contextlib import asynccontextmanager

    from daimon.core import channel_backend

    service, agent, actor, _, captured, _ = switching
    locked, seen = [False], []
    write = channel_backend.policy_store.policy_write_transaction

    @asynccontextmanager
    async def watched(*args, **kwargs):
        async with write(*args, **kwargs) as session:
            locked[0] = True
            try:
                yield session
            finally:
                locked[0] = False

    async def capture(actual):
        seen.append(locked[0])
        return captured[0]

    monkeypatch.setattr(channel_backend.policy_store, "policy_write_transaction", watched)
    service._capture_source = capture
    await service.preview(agent, "openai")
    await service.set_backend(agent, "openai", 0, actor)
    assert seen == [False, False], "the preview and the change capture unlocked"
