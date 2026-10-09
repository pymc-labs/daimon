"""C01–C18 from the pinned sprint matrix; store/host gaps remain explicit."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from mux.conformance.runner import Result, ScriptedTransport, StateStore
from mux.contracts.actions import NativeInput, UserMessage
from mux.contracts.config import CapabilityRequirement, ConfigRevision, ResolvedBackend
from mux.contracts.errors import (
    ContinuityLost,
    ExtensionVersionError,
    MigrationUnsupported,
    ProviderError,
    ScopeViolation,
    UnsupportedCapability,
)
from mux.contracts.events import (
    AgentMessagePayload,
    Event,
    RequiresActionPayload,
    TextPart,
    TurnEndedPayload,
)
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import PageRequest
from mux.contracts.ports import ManagedAgents, Steering
from mux.contracts.resources import Artifact

Probe = Callable[[ManagedAgents, StateStore | None, ScriptedTransport], Awaitable[Result]]


def passed(id_: str, *evidence: str) -> Result:
    return Result(id_, "pass", evidence)


def pending(id_: str, reason: str) -> Result:
    return Result(id_, "pending", (reason,))


async def c01(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending("C01", "N2 binding/lease store and N10 multi-human attribution seam pending")


async def c02(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C02")
    artifacts = await ma.artifacts.list(s.scope, s.session.ref, page=PageRequest())
    assert artifacts.data, "scenario must contain a binary workspace artifact"
    before = b"".join(
        [chunk async for chunk in ma.artifacts.download(s.scope, artifacts.data[0].ref)]
    )
    assert before == bytes(range(256)), "exact seeded binary bytes must survive"
    for index in range(2):
        await ma.events.send(
            s.scope,
            s.session.ref,
            (UserMessage(content=(TextPart(text=f"persistence-turn-{index}"),)),),
            key=f"turn-{index}",
        )
        journal = [event async for event in ma.events.stream(s.scope, s.session.ref)]
        assert any(e.type == "session.turn_ended" for e in journal)
        current = b"".join(
            [chunk async for chunk in ma.artifacts.download(s.scope, artifacts.data[0].ref)]
        )
        assert current == before, "workspace bytes changed between turns"
    history = await ma.events.list(s.scope, s.session.ref, page=PageRequest())
    assert sum(e.type == "user.message" for e in history.data) == 2
    for fault in ("expiry", "unexpected_loss"):
        t.fault(fault)
        writes = t.mutation_count
        try:
            await ma.sessions.retrieve(s.scope, s.session.ref)
        except ContinuityLost as exc:
            assert exc.binding_id == s.session.binding.id and exc.evidence
        else:
            raise AssertionError("loss must not silently start a fresh workspace")
        assert t.mutation_count == writes
    return passed(
        "C02", "256 binary bytes preserved", "expiry and unexpected loss typed and visible"
    )


async def c03(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending(
        "C03", "N2 persisted operation intent/digest and unknown-send reconciliation pending"
    )


async def c04(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending(
        "C04", "N2 restartable store, operation recovery and stale-fence commit seam pending"
    )


async def c05(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C05")
    # Adapter scripts disconnect, overlapping saved pages, a lost domain and child-first end.
    _ = [e async for e in ma.events.stream(s.scope, s.session.ref, previews=True)]
    projection = await ma.events.reconcile(s.scope, s.session.ref)
    events: list[Event] = []
    cursor = None
    cursors: set[str] = set()
    for _ in range(100):
        page = await ma.events.list(
            s.scope, s.session.ref, page=PageRequest(cursor=cursor, limit=2)
        )
        events.extend(page.data)
        cursor = page.next_cursor
        if cursor is None:
            break
        assert cursor not in cursors, "pagination cursor loop"
        cursors.add(cursor)
    else:
        raise AssertionError("pagination exceeded bound")
    assert len({e.id for e in events}) == len(events)
    messages = [e.typed_payload() for e in events if e.type == "agent.message"]
    assert messages and all(isinstance(m, AgentMessagePayload) for m in messages)
    ids = [m.item_id for m in messages if isinstance(m, AgentMessagePayload)]
    assert len(set(ids)) == len(ids), "saved/buffered item duplicated"
    ends = [e for e in events if e.type == "session.turn_ended" and e.thread_id is None]
    assert len(ends) == 1 and ends[0].authority in ("record", "reconciled")
    end = ends[0].typed_payload()
    assert (
        isinstance(end, TurnEndedPayload)
        and end.root_turn_id == "root"
        and end.outcome == "completed"
    )
    assert projection.state == "idle" and projection.active_root_turn is None
    assert projection.gaps and any(e.type == "session.history_gap" for e in events)
    return passed(
        "C05", "paged journal has unique items, explicit gap and one authoritative root outcome"
    )


async def c06(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C06")
    receipt = await ma.events.cancel(s.scope, s.session.ref, turn_id="root", key="cancel")
    assert receipt.status == "requested"
    stop = await ma.events.wait_stopped(s.scope, receipt, deadline=datetime.now(UTC))
    assert not stop.stopped and stop.outcome is None, "EOF is not observed termination"
    session = await ma.sessions.retrieve(s.scope, s.session.ref)
    assert session.state == "running" and session.active_root_turn == "root"
    t.fault("observed_stop")
    stop = await ma.events.wait_stopped(s.scope, receipt, deadline=datetime.now(UTC))
    assert stop.stopped and stop.outcome == "interrupted"
    session = await ma.sessions.retrieve(s.scope, s.session.ref)
    assert session.state == "idle" and session.active_root_turn is None
    return passed("C06", "cancel receipt/EOF held occupancy until observed interrupted outcome")


async def c07(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending("C07", "N2 revisioned usage/outbox and N8 signed-delta accounting seam pending")


async def c08(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C08")
    plan = await ma.sessions.plan_update(s.scope, s.session.ref, s.desired)
    assert plan.action == "next_turn" and plan.operations
    before = await ma.sessions.retrieve(s.scope, s.session.ref)
    t.fault("mount_add_failed_after_delete")
    try:
        receipt = await ma.sessions.apply_update(
            s.scope, plan, expected=before.effective_revision, key="update"
        )
    except ProviderError:
        pass
    else:
        assert receipt.status == "failed", "partial required mount cannot report success"
    after = await ma.sessions.retrieve(s.scope, s.session.ref)
    assert after.effective_revision == before.effective_revision
    assert after.state == "provisioning", "partial failure must block preparation"
    t.fault("mount_reconciled")
    await ma.events.reconcile(s.scope, s.session.ref)
    after = await ma.sessions.retrieve(s.scope, s.session.ref)
    assert (
        after.effective_revision.local > before.effective_revision.local and after.state == "idle"
    )
    return passed(
        "C08", "failed add retained effective revision and blocked preparation until reconciliation"
    )


async def c09(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C09")
    artifacts: list[Artifact] = []
    cursor = None
    cursors: set[str] = set()
    for _ in range(100):
        page = await ma.artifacts.list(
            s.scope, s.session.ref, page=PageRequest(cursor=cursor, limit=1)
        )
        artifacts.extend(page.data)
        cursor = page.next_cursor
        if cursor is None:
            break
        assert cursor not in cursors
        cursors.add(cursor)
    else:
        raise AssertionError("artifact pagination exceeded bound")
    assert len(artifacts) == 2 and len({a.ref.id for a in artifacts}) == 2
    body = b"".join([chunk async for chunk in ma.artifacts.download(s.scope, artifacts[0].ref)])
    assert hashlib.sha256(body).digest() == hashlib.sha256(bytes(range(256))).digest()
    t.fault("download_interrupted")
    try:
        _ = b"".join([chunk async for chunk in ma.artifacts.download(s.scope, artifacts[0].ref)])
    except ProviderError as exc:
        assert exc.category == "transient_network"
    else:
        raise AssertionError("interrupted download must resume exactly or fail explicitly")
    deletion = await ma.sessions.delete(s.scope, s.session.ref, key="delete")
    assert s.session.ref in deletion.deleted
    assert any(r.kind == "vault" for r in deletion.retained)
    assert all(r.kind != "vault" for r in deletion.deleted)
    return passed(
        "C09", "all artifact pages, binary checksum, typed download failure, shared vault retained"
    )


async def c10(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C10")
    writes = t.mutation_count
    for support in ("unsupported", "unknown"):
        t.fault(f"admission_{support}")
        profile = ma.capabilities()
        config = ConfigRevision.create(
            s.session.binding.thread.channel,
            1,
            ResolvedBackend(
                backend=profile.provider,
                profile=profile.profile_id,
                model="fixture",
                requires={"memory_stores": CapabilityRequirement(level="required")},
            ),
        )
        try:
            ma.admit(config)
        except UnsupportedCapability as exc:
            assert "memory_stores" in exc.missing
        else:
            raise AssertionError("unsupported/unknown requirement admitted")
    offered = ma.capabilities().extensions
    if offered:
        try:
            ma.extension(Steering, namespace=offered[0].namespace, version=999)
        except ExtensionVersionError:
            pass
        else:
            raise AssertionError("invalid extension version admitted")
    try:
        await ma.sessions.retrieve(s.foreign_scope, s.session.ref)
    except ScopeViolation:
        pass
    else:
        raise AssertionError("foreign tenant reference admitted")
    assert t.mutation_count == writes
    return passed(
        "C10", "unknown/unsupported requirements and foreign refs refused before mutation"
    )


async def c11(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C11")
    agent = await ma.agents.retrieve(s.scope, s.desired.agent)
    assert len(agent.spec.skills) == 1 and agent.spec.skills[0].digest == "sha256:fixture-bundle"
    bundle = await ma.skills.retrieve(s.scope, agent.spec.skills[0])
    assert bundle.artifact is not None and any(f.path == "SKILL.md" for f in bundle.files)
    assert agent.spec.mcp_servers and s.desired.environment is not None
    environment = await ma.environments.retrieve(s.scope, s.desired.environment)
    assert any(source.kind == "repository" for source in environment.spec.sources)
    journal = [e async for e in ma.events.stream(s.scope, s.session.ref)]
    actions = [e.typed_payload() for e in journal if e.type == "session.requires_action"]
    assert actions and all(isinstance(a, RequiresActionPayload) and a.actions for a in actions)
    assert not any(
        e.type == "session.turn_ended" and e.payload.get("outcome") == "completed" for e in journal
    )
    session = await ma.sessions.retrieve(s.scope, s.session.ref)
    assert session.state == "requires_action" and session.required_actions
    return passed(
        "C11", "pinned bundle, MCP and repository mounts observed; unavailable action explicit"
    )


async def c12(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending(
        "C12", "N8 historical ledger identity and overlapping-grain billing bridge pending"
    )


async def c13(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending("C13", "N2 atomic new-binding race seam pending")


async def c14(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending("C14", "N2 binding store and N10 backend selection/registry seam pending")


async def c15(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C15")
    before = await ma.sessions.retrieve(s.scope, s.session.ref)
    profile = ma.capabilities()
    target = ConfigRevision.create(
        before.binding.thread.channel,
        2,
        ResolvedBackend(backend=profile.provider, profile=profile.profile_id, model="fixture"),
    )
    writes = t.mutation_count
    try:
        await ma.sessions.migrate(
            s.scope, s.session.ref, target, expected=before.binding.generation, key="migrate"
        )
    except MigrationUnsupported:
        pass
    else:
        raise AssertionError("migration unexpectedly supported")
    assert (await ma.sessions.retrieve(s.scope, s.session.ref)).binding == before.binding
    assert t.mutation_count == writes
    return passed("C15", "migration typed unsupported, binding unchanged, no provider writes")


async def c16(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C16")
    for target in (ma, ma.events, ma.sessions):
        for name in ("client", "raw_client", "sdk", "raw"):
            try:
                value = getattr(target, name, None)
            except UnsupportedCapability:
                continue
            assert value is None, "raw provider handle is public"
    writes = t.mutation_count
    before = await ma.events.list(s.scope, s.session.ref, page=PageRequest())
    try:
        ma.extension(Steering, namespace=f"{ma.capabilities().provider}.bypass", version=1)
    except UnsupportedCapability:
        pass
    else:
        raise AssertionError("undeclared extension admitted")
    try:
        await ma.events.send(
            s.scope,
            s.session.ref,
            (
                NativeInput(
                    extension=ExtensionConfig(
                        namespace=f"{ma.capabilities().provider}.bypass", version=1, value={}
                    )
                ),
            ),
            key="bypass",
        )
    except UnsupportedCapability:
        pass
    else:
        raise AssertionError("native input bypassed extension admission")
    assert t.mutation_count == writes
    after = await ma.events.list(s.scope, s.session.ref, page=PageRequest())
    assert after == before, "rejected extension changed journal"
    return passed(
        "C16",
        "no public raw handle, undeclared namespace/native-input bypass refused before writes",
    )


async def c17(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending("C17", "N2 binding fence and N5 host wake generation seam pending")


async def c18(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending(
        "C18", "N4/N8 host TerminationReason/outcome-row adapter pending; mux cannot import daimon"
    )


FIXTURES: dict[str, Probe] = {
    f"C{i:02}": probe
    for i, probe in enumerate(
        (c01, c02, c03, c04, c05, c06, c07, c08, c09, c10, c11, c12, c13, c14, c15, c16, c17, c18),
        1,
    )
}
