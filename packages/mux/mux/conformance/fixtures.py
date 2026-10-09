"""C01–C18 from the pinned sprint matrix; store/host gaps remain explicit."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from mux.conformance.runner import (
    ConformanceFailure,
    Result,
    ScriptedTransport,
    StateStore,
    require,
)
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
    ToolResultPayload,
    TurnEndedPayload,
)
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import PageRequest
from mux.contracts.ports import ManagedAgents, Steering
from mux.contracts.resources import Artifact, SkillUpload, SkillUploadFile

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
    require(artifacts.data, "C02: scenario must contain a binary workspace artifact")
    before = b"".join(
        [chunk async for chunk in ma.artifacts.download(s.scope, artifacts.data[0].ref)]
    )
    require(before == bytes(range(256)), "C02: exact seeded binary bytes must survive")
    for index in range(2):
        await ma.events.send(
            s.scope,
            s.session.ref,
            (UserMessage(content=(TextPart(text=f"persistence-turn-{index}"),)),),
            key=f"turn-{index}",
        )
        journal = [event async for event in ma.events.stream(s.scope, s.session.ref)]
        require(
            any(e.type == "session.turn_ended" for e in journal),
            "C02: turn must have an authoritative end event",
        )
        current = b"".join(
            [chunk async for chunk in ma.artifacts.download(s.scope, artifacts.data[0].ref)]
        )
        require(current == before, "C02: workspace bytes changed between turns")
    history = await ma.events.list(s.scope, s.session.ref, page=PageRequest())
    require(
        sum(e.type == "user.message" for e in history.data) == 2,
        "C02: both turn inputs must survive in history",
    )
    expiry_evidence: tuple[str, ...] | None = None
    for fault in ("expiry", "unexpected_loss"):
        t.fault(fault)
        writes = t.mutation_count
        try:
            await ma.sessions.retrieve(s.scope, s.session.ref)
        except ContinuityLost as exc:
            if fault == "expiry":
                require(
                    any("expir" in item.lower() for item in exc.evidence),
                    "C02: expiry must be disclosed explicitly",
                )
                expiry_evidence = exc.evidence
            else:
                require(
                    exc.evidence != expiry_evidence,
                    "C02: unexpected loss must be distinguished from expiry",
                )
            require(
                exc.binding_id == s.session.binding.id and exc.evidence,
                "C02: continuity loss must identify the binding and provide evidence",
            )
        else:
            raise ConformanceFailure("loss must not silently start a fresh workspace")
        require(
            t.mutation_count == writes, "C02: continuity loss must not create provider resources"
        )
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
    buffered = [e async for e in ma.events.stream(s.scope, s.session.ref, previews=True)]
    running = await ma.sessions.retrieve(s.scope, s.session.ref)
    if running.state != "running" or running.active_root_turn != "root":
        raise ConformanceFailure("child completion or EOF prematurely released the root turn")
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
        require(cursor not in cursors, "C05: pagination cursor loop")
        cursors.add(cursor)
    else:
        raise ConformanceFailure("pagination exceeded bound")
    require(
        len({e.id for e in events}) == len(events),
        "C05: journal entries must have unique IDs",
    )
    messages = [e.typed_payload() for e in events if e.type == "agent.message"]
    require(
        messages and all(isinstance(m, AgentMessagePayload) for m in messages),
        "C05: saved journal must contain typed messages",
    )
    ids = [m.item_id for m in messages if isinstance(m, AgentMessagePayload)]
    require(len(set(ids)) == len(ids), "C05: saved/buffered item duplicated")
    require(len(messages) == 1, "C05: expected exactly one saved message")
    message = messages[0]
    if not (isinstance(message, AgentMessagePayload)):
        raise ConformanceFailure("C05: message payload must be typed")
    require(
        message.item_id == "item" and message.content == (TextPart(text="done"),),
        "C05: saved message identity or text was altered",
    )
    require(message.complete, "C05: saved final message must preserve complete content")
    results: dict[tuple[str | None, str], ToolResultPayload] = {}
    for event in events:
        if event.type != "agent.tool_result":
            continue
        payload = event.typed_payload()
        if not (isinstance(payload, ToolResultPayload)):
            raise ConformanceFailure("C05: tool result payload must be typed")
        identity = (event.thread_id, payload.call_id)
        require(identity not in results, "C05: saved/buffered tool result duplicated")
        results[identity] = payload
    require(set(results) == {(None, "call")}, "C05: scripted tool result missing or altered")
    require(
        results[(None, "call")].content == (TextPart(text="result"),),
        "C05: saved tool result content was altered",
    )
    require(
        not results[(None, "call")].is_error, "C05: scripted successful tool result became an error"
    )
    ends = [e for e in events if e.type == "session.turn_ended" and e.thread_id is None]
    require(
        len(ends) == 1 and ends[0].authority in ("record", "reconciled"),
        "C05: expected one authoritative root-turn end",
    )
    end = ends[0].typed_payload()
    require(
        isinstance(end, TurnEndedPayload)
        and end.root_turn_id == "root"
        and end.outcome == "completed",
        "C05: authoritative root outcome must be completed",
    )
    require(
        projection.state == "idle" and projection.active_root_turn is None,
        "C05: observed root end must release occupancy",
    )
    require(
        projection.gaps and any(e.type == "session.history_gap" for e in events),
        "C05: unrecoverable history loss must have an explicit gap",
    )
    saved_ids = {m.item_id for m in messages if isinstance(m, AgentMessagePayload)}
    for event in buffered:
        if event.type == "agent.message":
            payload = event.typed_payload()
            if not isinstance(payload, AgentMessagePayload) or payload.item_id not in saved_ids:
                raise ConformanceFailure("streamed message missing from reconciled saved items")
    return passed(
        "C05", "paged journal has unique items, explicit gap and one authoritative root outcome"
    )


async def c06(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C06")
    receipt = await ma.events.cancel(s.scope, s.session.ref, turn_id="root", key="cancel")
    require(receipt.status == "requested", "C06: cancel receipt must acknowledge only the request")
    stop = await ma.events.wait_stopped(
        s.scope, receipt, deadline=datetime.now(UTC) + timedelta(seconds=5)
    )
    require(not stop.stopped and stop.outcome is None, "C06: EOF is not observed termination")
    session = await ma.sessions.retrieve(s.scope, s.session.ref)
    require(
        session.state == "running" and session.active_root_turn == "root",
        "C06: cancel request without stop must hold root occupancy",
    )
    t.fault("observed_stop")
    stop = await ma.events.wait_stopped(
        s.scope, receipt, deadline=datetime.now(UTC) + timedelta(seconds=5)
    )
    require(
        stop.stopped and stop.outcome == "interrupted",
        "C06: interrupted outcome must be observed before release",
    )
    session = await ma.sessions.retrieve(s.scope, s.session.ref)
    require(
        session.state == "idle" and session.active_root_turn is None,
        "C06: observed stop must release root occupancy",
    )
    return passed("C06", "cancel receipt/EOF held occupancy until observed interrupted outcome")


async def c07(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    return pending("C07", "N2 revisioned usage/outbox and N8 signed-delta accounting seam pending")


async def c08(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C08")
    plan = await ma.sessions.plan_update(s.scope, s.session.ref, s.desired)
    require(
        plan.action == "next_turn" and plan.operations,
        "C08: tool/mount mutation must be planned for the next turn",
    )
    before = await ma.sessions.retrieve(s.scope, s.session.ref)
    t.fault("mount_add_failed_after_delete")
    try:
        receipt = await ma.sessions.apply_update(
            s.scope, plan, expected=before.effective_revision, key="update"
        )
    except ProviderError:
        pass
    else:
        require(receipt.status == "failed", "C08: partial required mount cannot report success")
    after = await ma.sessions.retrieve(s.scope, s.session.ref)
    require(
        after.effective_revision == before.effective_revision,
        "C08: partial mutation must not advance the effective revision",
    )
    require(after.state == "provisioning", "C08: partial failure must block preparation")
    t.fault("mount_reconciled")
    await ma.events.reconcile(s.scope, s.session.ref)
    after = await ma.sessions.retrieve(s.scope, s.session.ref)
    require(
        after.effective_revision.local > before.effective_revision.local and after.state == "idle",
        "C08: reconciled required mounts must advance revision and unblock preparation",
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
        require(cursor not in cursors, "C09: artifact pagination cursor loop")
        cursors.add(cursor)
    else:
        raise ConformanceFailure("artifact pagination exceeded bound")
    require(
        len(artifacts) == 2 and len({a.ref.id for a in artifacts}) == 2,
        "C09: artifact discovery must return both unique files",
    )
    body = b"".join([chunk async for chunk in ma.artifacts.download(s.scope, artifacts[0].ref)])
    require(
        hashlib.sha256(body).digest() == hashlib.sha256(bytes(range(256))).digest(),
        "C09: downloaded binary checksum must match seeded bytes",
    )
    t.fault("download_interrupted")
    try:
        resumed = b"".join(
            [chunk async for chunk in ma.artifacts.download(s.scope, artifacts[0].ref)]
        )
    except ProviderError as exc:
        require(
            exc.category == "transient_network",
            "C09: interrupted download must return a typed network failure",
        )
    else:
        require(resumed == body, "C09: resumed download truncated, duplicated or corrupted bytes")
    require(s.shared_resources, "C09: scenario must seed a shared channel resource")
    deletion = await ma.sessions.delete(s.scope, s.session.ref, key="delete")
    require(
        all(ref in deletion.retained for ref in s.shared_resources),
        "C09: receipt must retain the exact seeded shared resources",
    )
    require(
        all(ref not in t.deleted_resources for ref in s.shared_resources),
        "C09: provider must not actually delete shared resources",
    )
    require(
        s.session.ref in deletion.deleted, "C09: deletion receipt must name the deleted session"
    )
    require(
        any(r.kind == "vault" for r in deletion.retained),
        "C09: shared vault must be retained",
    )
    require(
        all(r.kind != "vault" for r in deletion.deleted),
        "C09: shared vault must not be deleted",
    )
    return passed(
        "C09", "all artifact pages, exact resume or typed download failure, shared vault retained"
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
            require(
                "memory_stores" in exc.missing,
                "C10: admission refusal must name the unmet capability",
            )
        else:
            raise ConformanceFailure("unsupported/unknown requirement admitted")
    offered = ma.capabilities().extensions
    if offered:
        try:
            ma.extension(Steering, namespace=offered[0].namespace, version=999)
        except ExtensionVersionError:
            pass
        else:
            raise ConformanceFailure("invalid extension version admitted")
    try:
        await ma.sessions.retrieve(s.foreign_scope, s.session.ref)
    except ScopeViolation:
        pass
    else:
        raise ConformanceFailure("foreign tenant reference admitted")
    require(t.mutation_count == writes, "C10: capability and scope rejection must precede writes")
    return passed(
        "C10",
        "unknown/unsupported requirements and foreign refs refused before mutation",
        "invalid extension version refused"
        if offered
        else "extension version check not applicable: profile offers no extensions",
    )


async def c11(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C11")
    deployed = await ma.skills.create(
        s.scope,
        SkillUpload(
            display_title="fixture",
            files=(
                SkillUploadFile(path="SKILL.md", content=b"fixture", media_type="text/markdown"),
            ),
        ),
        key="skill-deploy",
    )
    pinned = deployed.latest_version
    if pinned is None or pinned.version is None:
        raise ConformanceFailure("C11: uploaded bundle must return a pinned skill version")
    require(
        pinned.id == deployed.id
        and pinned.digest == f"sha256:{hashlib.sha256(b'fixture').hexdigest()}",
        "C11: uploaded bundle must preserve the seeded content digest",
    )
    agent = await ma.agents.retrieve(s.scope, s.desired.agent)
    skills = agent.spec.skills
    require(
        skills is not None and len(skills) == 1 and skills[0] == pinned,
        "C11: deployed agent must bind the uploaded pinned skill version",
    )
    skill = await ma.skills.retrieve(s.scope, pinned.id)
    require(
        skill.id == pinned.id and skill.latest_version == pinned,
        "C11: retrieved skill record must preserve the deployed pin",
    )
    if not (agent.spec.mcp_servers and s.desired.environment is not None):
        raise ConformanceFailure("C11: deployed MCP and environment bindings are required")
    environment = await ma.environments.retrieve(s.scope, s.desired.environment)
    require(
        any(source.kind == "repository" for source in (environment.spec.sources or ())),
        "C11: required repository mount must be present",
    )
    journal = [e async for e in ma.events.stream(s.scope, s.session.ref)]
    actions = [e.typed_payload() for e in journal if e.type == "session.requires_action"]
    require(
        actions and all(isinstance(a, RequiresActionPayload) and a.actions for a in actions),
        "C11: unavailable execution must surface typed required actions",
    )
    require(
        not any(
            e.type == "session.turn_ended" and e.payload.get("outcome") == "completed"
            for e in journal
        ),
        "C11: unavailable execution must not claim successful completion",
    )
    session = await ma.sessions.retrieve(s.scope, s.session.ref)
    require(
        session.state == "requires_action" and session.required_actions,
        "C11: session must stay occupied by the unavailable action",
    )
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
        raise ConformanceFailure("migration unexpectedly supported")
    require(
        (await ma.sessions.retrieve(s.scope, s.session.ref)).binding == before.binding,
        "C15: rejected migration must leave the binding unchanged",
    )
    require(t.mutation_count == writes, "C15: unsupported migration must not write to the provider")
    return passed("C15", "migration typed unsupported, binding unchanged, no provider writes")


async def c16(ma: ManagedAgents, store: StateStore | None, t: ScriptedTransport) -> Result:
    s = await t.arrange("C16")
    for target in (ma, ma.events, ma.sessions):
        for name in ("client", "raw_client", "sdk", "raw"):
            try:
                value = getattr(target, name, None)
            except UnsupportedCapability:
                continue
            require(value is None, "C16: raw provider handle is public")
    writes = t.mutation_count
    before = await ma.events.list(s.scope, s.session.ref, page=PageRequest())
    try:
        ma.extension(Steering, namespace=f"{ma.capabilities().provider}.bypass", version=1)
    except UnsupportedCapability:
        pass
    else:
        raise ConformanceFailure("undeclared extension admitted")
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
        raise ConformanceFailure("native input bypassed extension admission")
    require(
        t.mutation_count == writes, "C16: rejected extension bypass must not write to the provider"
    )
    after = await ma.events.list(s.scope, s.session.ref, page=PageRequest())
    require(after == before, "C16: rejected extension changed journal")
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
