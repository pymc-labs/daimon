"""Confirmed losses belong to the existing policy/CAS/audit transaction."""

import asyncio

import pytest
from daimon.core.channel_backend import AgentBackendRefused, AgentBackendService
from daimon.core.stores import security_audit
from daimon.core.turn.runtime_manifest import source_preservation_pin

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
async def test_preview_confirmation_and_audit_retain_the_exact_source(switching, provider):
    service, agent, actor, sessions, captured, reads = switching
    preview = await service.preview(agent, provider)
    assert preview.allowed and preview.declared_drops == ("sandbox",)
    assert preview.drop_consent.source_pin == source_preservation_pin(captured[0])
    revision = await service.set_backend(
        agent,
        provider,
        0,
        actor,
        accepted_drops=preview.declared_drops,
        expected_preview_digest=preview.digest,
    )
    assert reads == [agent, agent]
    assert revision.drop_consent == preview.drop_consent
    assert revision.preview_digest == preview.digest
    assert await service.revision(agent, 1) == revision
    async with sessions() as db:
        audit = await security_audit.list_events(db, tenant_id=agent.tenant_id)
    assert len(audit) == 2
    assert audit[-1].operation == "set"
    assert audit[-1].reason == "completed; declared_drops=sandbox"
    assert audit[-1].account_id == actor.account_id


@pytest.mark.parametrize(
    "accepted,digest,reason",
    [
        (None, True, "drops required"),
        ((), True, "drops mismatch"),
        (("sandbox", "memory"), True, "drops mismatch"),
        (("credentials",), True, "drops mismatch"),
        (("sandbox",), False, "stale preview"),
    ],
)
async def test_unconfirmed_losses_never_insert_or_audit(switching, accepted, digest, reason):
    service, agent, actor, _, _, _ = switching
    preview = await service.preview(agent, "openai")
    before = await service.audit(agent.tenant_id)
    with pytest.raises(AgentBackendRefused, match=reason):
        await service.set_backend(
            agent,
            "openai",
            0,
            actor,
            accepted_drops=accepted,
            expected_preview_digest=preview.digest if digest else None,
        )
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id) == before


async def test_same_loss_labels_with_new_source_content_require_confirmation(switching):
    service, agent, actor, _, captured, _ = switching
    preview = await service.preview(agent, "openai")
    captured[0] = captured[0].model_copy(
        update={
            "effective_spec": captured[0].effective_spec.model_copy(
                update={"system": "New authoritative instruction"}
            )
        }
    )
    replacement = await service.preview(agent, "openai")
    assert replacement.declared_drops == preview.declared_drops
    assert replacement.digest != preview.digest
    assert replacement.drop_consent.source_pin != preview.drop_consent.source_pin
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await service.set_backend(
            agent,
            "openai",
            0,
            actor,
            accepted_drops=preview.declared_drops,
            expected_preview_digest=preview.digest,
        )
    assert await service.current(agent) is None


async def test_concurrent_confirmed_switches_have_one_revision_and_audit(switching):
    service, agent, actor, _, _, _ = switching
    preview = await service.preview(agent, "openai")

    async def change():
        return await service.set_backend(
            agent,
            "openai",
            0,
            actor,
            accepted_drops=preview.declared_drops,
            expected_preview_digest=preview.digest,
        )

    results = await asyncio.gather(change(), change(), return_exceptions=True)
    assert sum(isinstance(value, AgentBackendRefused) for value in results) == 1
    assert (await service.current(agent)).local == 1
    audit = await service.audit(agent.tenant_id)
    assert [row.operation for row in audit] == ["allowlist", "set"]


async def test_clear_removes_consent_and_revert_reproves_the_original(switching):
    service, agent, actor, _, captured, reads = switching
    preview = await service.preview(agent, "openai")
    original = await service.set_backend(
        agent,
        "openai",
        0,
        actor,
        accepted_drops=preview.declared_drops,
        expected_preview_digest=preview.digest,
    )
    cleared = await service.clear_backend(agent, 1, actor)
    assert cleared.backend is None and cleared.drop_consent is None
    quote = await service.preview(agent, "openai")
    restored = await service.revert(
        agent, actor, expected_revision=2, expected_preview_digest=quote.digest
    )
    assert restored.drop_consent == original.drop_consent
    assert len(reads) == 4
    await service.clear_backend(agent, 3, actor)
    captured[0] = captured[0].model_copy(update={"memory_required": True})
    with pytest.raises(AgentBackendRefused, match="re confirm"):
        await service.revert(agent, actor, expected_revision=4)
    assert (await service.current(agent)).local == 4


@pytest.mark.parametrize("publishing", [None, True])
async def test_unknown_or_required_publishing_approval_is_never_dropped(switching, publishing):
    service, agent, actor, _, captured, _ = switching
    captured[0] = captured[0].model_copy(update={"asks_before_publishing": publishing})
    with pytest.raises(AgentBackendRefused, match="source unsupported"):
        await service.preview(agent, "openai")
    assert await service.current(agent) is None
    assert [row.operation for row in await service.audit(agent.tenant_id)] == ["allowlist"]


@pytest.mark.parametrize("recapture", ["missing", "no_loss"])
async def test_revert_cannot_remove_the_original_confirmation(switching, recapture):
    service, agent, actor, sessions, captured, _ = switching
    preview = await service.preview(agent, "openai")
    await service.set_backend(
        agent,
        "openai",
        0,
        actor,
        accepted_drops=preview.declared_drops,
        expected_preview_digest=preview.digest,
    )
    await service.clear_backend(agent, 1, actor)
    if recapture == "missing":
        service = AgentBackendService(sessions, enabled=True)
    else:
        captured[0] = captured[0].model_copy(
            update={
                "effective_spec": captured[0].effective_spec.model_copy(
                    update={
                        "model": captured[0].effective_spec.model.model_copy(
                            update={"provider": "openai", "id": "gpt-6-luna"}
                        )
                    }
                )
            }
        )
    before = await service.audit(agent.tenant_id)
    with pytest.raises(AgentBackendRefused, match="re confirm"):
        await service.revert(agent, actor, expected_revision=2)
    assert (await service.current(agent)).local == 2
    assert await service.audit(agent.tenant_id) == before


async def test_a_switch_claiming_drops_against_an_undeclared_backend_refuses(owned):
    """The service-side guard against a caller claiming accepted drops when the
    real preview declares none (defense-in-depth alongside the chat card's own
    recheck in `confirmation.py`): `owned`'s service has no `capture_source`, so
    its preview always declares no drops.
    """
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    preview = await service.preview(agent, "openai")
    assert not preview.declared_drops and preview.drop_consent is None
    before = await service.audit(agent.tenant_id)
    with pytest.raises(AgentBackendRefused, match="source unavailable"):
        await service.set_backend(
            agent,
            "openai",
            0,
            actor,
            accepted_drops=("memory",),
            expected_preview_digest=preview.digest,
        )
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id) == before


async def test_revert_cannot_add_consent_to_an_unconfirmed_original(switching):
    service, agent, actor, sessions, _, reads = switching
    legacy = AgentBackendService(sessions, enabled=True)
    original = await legacy.set_backend(agent, "openai", 0, actor)
    assert original.drop_consent is None
    await legacy.clear_backend(agent, original.local, actor)
    before = await service.audit(agent.tenant_id)
    with pytest.raises(AgentBackendRefused, match="re confirm"):
        await service.revert(agent, actor, expected_revision=2)
    assert len(reads) == 1
    assert (await service.current(agent)).local == 2
    assert await service.audit(agent.tenant_id) == before
