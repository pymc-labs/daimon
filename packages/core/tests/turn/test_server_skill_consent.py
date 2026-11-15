"""A followed skill update renews a backend consent only when its drops stay the same."""

import datetime as dt
from types import SimpleNamespace

import pytest
from daimon.core.channel_backend import AgentBackendService
from daimon.core.server_skills.consent import (
    ConsentKeeper,
    Renewer,
    repin_residue,
    with_skill_version,
)
from daimon.core.server_skills.sync import NeedsReconfirm
from daimon.core.turn import runtime_manifest
from daimon.core.turn.runtime_manifest import source_preservation_pin
from mux.contracts.ids import Revision, SkillRef
from mux.contracts.resources import ResourceBinding

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned
from .source_projection_support import captured_source

MIRROR = SimpleNamespace(skill_id="skill_1", digest="digest-2")


def _with_skill(source):
    skills = (SkillRef(id="skill_1", version="1", source="custom"),)
    spec = source.effective_spec.model_copy(update={"tools": (), "skills": skills})
    native = source.source.model_copy(update={"spec": spec})
    return source.model_copy(
        update={"effective_spec": spec, "source": native, "asks_before_publishing": False}
    )


async def _consent(backend, owned, *, before_readdress=False):
    """An agent on `backend` whose owner accepted its declared drops.

    `before_readdress` stores the consent as an earlier release did: pinned to the
    source with the consenter's own caller credential address, on a source that has one.
    """
    _, agent, actor, sessions = owned
    source = _with_skill(captured_source()).model_copy(
        update={"tenant_id": agent.tenant_id, "agent_id": agent.agent_id}
    )
    if before_readdress:
        reference = (
            f"caller-mcp:{source.tenant_id}:{source.resource_scope.account_id}:{source.agent_id}"
        )
        source = source.model_copy(
            update={
                "resource_requirements": (
                    ResourceBinding(id=reference, kind="credential", credential_ref=reference),
                )
            }
        )
    captured = [source]

    async def capture(_actual):
        return captured[0]

    service = AgentBackendService(sessions, enabled=True, capture_source=capture)
    await enable(service, agent, actor)
    with pytest.MonkeyPatch.context() as patch:
        if before_readdress:
            current = runtime_manifest.source_preservation_pin
            patch.setattr(
                runtime_manifest,
                "source_preservation_pin",
                lambda source, *, readdress=True: current(source, readdress=False),
            )
        preview = await service.preview(agent, backend)
        revision = await service.set_backend(
            agent,
            backend,
            0,
            actor,
            accepted_drops=preview.declared_drops,
            expected_preview_digest=preview.digest,
        )
    assert revision.drop_consent is not None, "the switch records a consent"
    if before_readdress:
        assert revision.drop_consent.source_pin != source_preservation_pin(source), (
            "the stored pin predates the readdressed formula"
        )

    async def renewer(_mirror):
        return Renewer(backends=service, account_id=actor.account_id)

    async def load(_mirror, _agent_id):
        return agent

    keeper = ConsentKeeper(sessionmaker=sessions, renewer=renewer, load=load)
    return keeper, service, agent, actor, captured, revision


@pytest.fixture(params=["openai", "gemini"])
async def consented(request, owned):
    """An agent on another provider whose owner accepted its declared drops."""
    return await _consent(request.param, owned)


@pytest.fixture(params=["openai", "gemini"])
async def consented_before_readdress(request, owned):
    """The same, with a consent stored before the caller credential was readdressed."""
    return await _consent(request.param, owned, before_readdress=True)


def _repin(captured):
    """The source as the provider reads it after a repin: a new version and record."""
    source = with_skill_version(captured[0], "skill_1", "2")
    native = source.source
    native = native.model_copy(
        update={
            "revision": Revision(local=2, native="2"),
            "updated_at": dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
            "native": {**native.native, "version": 2, "skills": [{"version": "2"}]},
        }
    )
    captured[0] = source.model_copy(update={"source": native, "configuration_digest": "c" * 64})


def _edit_system(captured, text):
    spec = captured[0].effective_spec.model_copy(update={"system": text})
    captured[0] = captured[0].model_copy(update={"effective_spec": spec})


async def test_same_drops_renew_the_consent_and_audit_it(consented):
    """The repinned agent keeps answering, under a renewal anyone can trace."""
    keeper, service, agent, actor, captured, revision = consented
    old_pin = revision.drop_consent.source_pin
    checked = await keeper.check(MIRROR, "agent", "2")
    assert checked is not None and checked.revision == revision, "the current consent"

    _repin(captured)
    await keeper.renew(MIRROR, "agent", checked)

    current = await service.current(agent)
    assert current is not None and current.local == revision.local + 1
    new_pin = source_preservation_pin(captured[0])
    assert current.drop_consent.source_pin == new_pin != old_pin, "pinned to the new source"
    assert current.drop_consent.declared_drops == revision.drop_consent.declared_drops
    assert (current.backend, current.model) == (revision.backend, revision.model)
    assert current.previous_local == revision.previous_local, "revert skips the renewal"
    assert current.preview_digest not in (None, revision.preview_digest), "a concrete new digest"
    assert current.drop_consent.target_digest, "the consent pins the new target"
    events = await service.audit(agent.tenant_id)
    renewal = events[-1]
    assert renewal.operation == "renew" and renewal.outcome == "allowed"
    assert renewal.account_id is None, "the sync renews, not a person"
    for part in (
        "actor=server_skill_sync",
        f"added_by={actor.account_id}",
        "agent=agent",
        "skill=skill_1",
        "digest=digest-2",
        f"source_pin={old_pin}->{new_pin}",
    ):
        assert part in renewal.reason, f"the audit names {part}"


async def test_a_consent_stored_before_the_readdress_still_renews(consented_before_readdress):
    """An owner who confirmed under the older pin is not asked again after a repin."""
    keeper, service, agent, _, captured, revision = consented_before_readdress
    old_pin = revision.drop_consent.source_pin
    assert await keeper.covers(MIRROR, "agent"), "the older pin still vouches for its source"
    checked = await keeper.check(MIRROR, "agent", "2")
    assert checked is not None and checked.revision == revision, "no NeedsReconfirm"

    _repin(captured)
    await keeper.renew(MIRROR, "agent", checked)

    current = await service.current(agent)
    assert current is not None and current.local == revision.local + 1, "renewed as a revision"
    assert current.drop_consent.source_pin == source_preservation_pin(captured[0]) != old_pin, (
        "the renewal stores the new pin"
    )
    assert await keeper.covers(MIRROR, "agent"), "the new pin covers the repinned source"


async def test_different_drops_hold_the_agent_untouched(consented, monkeypatch):
    """An update that would change the drops is never applied for the owner."""
    keeper, service, agent, _, _, revision = consented
    real = runtime_manifest.preview_source_drops

    def drops_memory_at_v2(source, model, *, effort=None):
        spec = real(source, model, effort=effort)
        if any(s.version == "2" for s in source.effective_spec.skills or ()):
            return spec.model_copy(update={"declared_drops": (*spec.declared_drops, "memory")})
        return spec

    monkeypatch.setattr(runtime_manifest, "preview_source_drops", drops_memory_at_v2)
    before = await service.audit(agent.tenant_id)
    with pytest.raises(NeedsReconfirm, match="different capabilities"):
        await keeper.check(MIRROR, "agent", "2")
    assert await service.current(agent) == revision, "the consent is unchanged"
    assert await service.audit(agent.tenant_id) == before, "nothing renewed, nothing audited"


async def test_a_consent_already_stale_is_never_renewed(consented):
    """A source the owner never confirmed stays for the owner to re-confirm."""
    keeper, service, agent, _, captured, revision = consented
    _edit_system(captured, "Changed after consent")
    with pytest.raises(NeedsReconfirm):
        await keeper.check(MIRROR, "agent", "2")
    assert await service.current(agent) == revision


async def test_drops_that_change_during_the_repin_refuse_the_renewal(consented, monkeypatch):
    """A race past the check leaves the consent stale, so turns ask the owner."""
    keeper, service, agent, _, captured, revision = consented
    checked = await keeper.check(MIRROR, "agent", "2")
    real = runtime_manifest.preview_source_drops
    monkeypatch.setattr(
        runtime_manifest,
        "preview_source_drops",
        lambda s, m, *, effort=None: real(s, m, effort=effort).model_copy(
            update={"declared_drops": ("memory",)}
        ),
    )
    _repin(captured)
    with pytest.raises(NeedsReconfirm):
        await keeper.renew(MIRROR, "agent", checked)
    assert await service.current(agent) == revision, "no renewal was written"


async def test_an_edit_racing_the_repin_is_never_renewed(consented):
    """An edit landing between the check and the repin stays for the owner to confirm."""
    keeper, service, agent, _, captured, revision = consented
    checked = await keeper.check(MIRROR, "agent", "2")
    assert checked is not None
    before = await service.audit(agent.tenant_id)
    _edit_system(captured, "Edited while the update ran")
    _repin(captured)
    with pytest.raises(NeedsReconfirm, match="changed since the check"):
        await keeper.renew(MIRROR, "agent", checked)
    assert await service.current(agent) == revision, "no renewal was written"
    assert await service.audit(agent.tenant_id) == before, "nothing renewed, nothing audited"


async def test_another_skill_changing_alongside_the_repin_is_never_renewed(consented):
    """Only the followed skill may move; any other skill change stays for the owner."""
    keeper, service, agent, _, captured, revision = consented
    checked = await keeper.check(MIRROR, "agent", "2")
    assert checked is not None
    _repin(captured)
    spec = captured[0].effective_spec
    skills = (*spec.skills, SkillRef(id="skill_2", version="7", source="custom"))
    spec = spec.model_copy(update={"skills": skills})
    native = captured[0].source.model_copy(update={"spec": spec})
    captured[0] = captured[0].model_copy(update={"effective_spec": spec, "source": native})
    with pytest.raises(NeedsReconfirm, match="changed since the check"):
        await keeper.renew(MIRROR, "agent", checked)
    assert await service.current(agent) == revision, "no renewal was written"


def test_an_unreadable_record_keeps_its_digest_in_the_residue():
    """Without a native record to blank, the record digest still tells repins apart."""
    source = _with_skill(captured_source())
    opaque = source.model_copy(
        update={"source": source.source.model_copy(update={"native": "opaque"})}
    )
    moved = opaque.model_copy(update={"configuration_digest": "d" * 64})
    assert repin_residue(opaque, "skill_1") != repin_residue(moved, "skill_1")


async def test_without_anyone_to_vouch_the_update_is_held(consented):
    """No active adder means no renewal, so a consented agent is held."""
    keeper, _, agent, _, _, _ = consented

    async def nobody(_mirror):
        return None

    orphan = ConsentKeeper(sessionmaker=keeper.sessionmaker, renewer=nobody, load=keeper.load)
    with pytest.raises(NeedsReconfirm, match="no one can renew"):
        await orphan.check(MIRROR, "agent", "2")


async def test_an_agent_without_a_consent_needs_no_renewal(owned):
    """An agent on its own provider is repinned without touching any consent."""
    service, agent, actor, sessions = owned

    async def renewer(_mirror):
        return Renewer(backends=service, account_id=actor.account_id)

    async def load(_mirror, _agent_id):
        return agent

    keeper = ConsentKeeper(sessionmaker=sessions, renewer=renewer, load=load)
    assert await keeper.check(MIRROR, "agent", "2") is None, "nothing to renew"
