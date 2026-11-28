"""A routine fire and an agent step reach their place through one admission."""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.scheduler.workflows import (
    HeadlessLaneTurn,
    ProviderLaneTurn,
    WorkflowEngine,
    lane_turn,
    run_once,
)
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.channel_backend import channel_ref
from daimon.core.fire import admit, run
from daimon.core.fire.run import FireDeps, fire_turn
from daimon.core.fire.store import FireStore
from daimon.core.fire.subject import routine_subject
from daimon.core.headless_runner import StepTurn
from daimon.core.lead_intake_execution import (
    WorkflowExecution,
    current_intake_execution,
    lane_grant,
)
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.provider_routine import (
    STEP_SHELL_NEEDS_GRANT,
    ProviderRoutineRun,
    workflow_scope_thread,
)
from daimon.core.routine_delivery import QUIET_RUN_SENTENCE
from daimon.core.stores import identity, tenants, workflow_deliveries, workflow_runs, workflows
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.routines import create_routine
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from daimon.core.turn.state import TurnState
from daimon.core.workflows import agent_step, engine, runs
from daimon.core.workflows.agent_step import AUTHORING_TOOLS, AgentStepExecutor, step_decider
from daimon.core.workflows.brief import render_brief
from daimon.core.workflows.lanes import lane_key
from daimon.core.workflows.spec import AgentNode
from daimon.testing.workflows import NOW, World, make_world
from mux.contracts.config import ConfigRevision
from mux.profiles import backend_selection
from sqlalchemy.ext.asyncio import AsyncSession

#: The checks `fire.admit.admit_place` owns: access, hold and origin.
OWNED = {"authorize", "routine_hold", "routine_origin", "lapsed_role"}
OWNER = "111"
HOME = "555"
STEP = {
    "a": {"kind": "agent", "agent": "helper", "instructions": "go", "at": "home", "next": "f"},
    "f": {"kind": "finish"},
}
GATED = TenantAccessPolicy(agent_rules={"helper": AgentRule(invoker_role_ids=("123",))})


def test_only_admit_place_runs_the_access_hold_and_origin_checks() -> None:
    for module in (run, lane_turn, agent_step):
        tree = ast.parse(Path(_file(module)).read_text())
        called = {
            node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not called & OWNED, f"{module.__name__} checks {called & OWNED} itself"
        assert "admit_place" in called or module is agent_step, module.__name__


def _file(module: ModuleType) -> str:
    assert module.__file__ is not None
    return module.__file__


def _member(roles: tuple[str, ...] = (), *, known: bool = True) -> CachedMembershipResolver:
    async def fetch(*_args: object) -> Membership:
        return Membership(is_member=True if known else None, role_ids=roles)

    return CachedMembershipResolver(fetch)


async def _world(
    db_session: AsyncSession, nodes: dict[str, Any] = STEP, policy: TenantAccessPolicy | None = None
) -> World:
    """A workflow homed in a Discord channel, its owner linked there as `OWNER`."""
    world = await make_world(db_session, nodes, "a", home=HOME, workspace_id="guild-1")
    async with world.sm.begin() as s:
        await identity.create_platform_principal(
            s, tenant_id=world.tenant_id, platform="discord", external_id=OWNER,
            account_id=world.owner,
        )  # fmt: skip
        if policy is not None:
            await set_access_policy(s, tenant_id=world.tenant_id, policy=policy)
    return world


def _lane(
    world: World, monkeypatch: pytest.MonkeyPatch, resolver: CachedMembershipResolver | None
) -> HeadlessLaneTurn:
    async def resolved(*_a: object, **_kw: object) -> object:
        return SimpleNamespace(environment_name=None)

    monkeypatch.setattr(lane_turn, "resolve_agent", AsyncMock(return_value="agent_a"))
    monkeypatch.setattr(lane_turn, "resolve_environment", AsyncMock(return_value="env_1"))
    monkeypatch.setattr(lane_turn, "resolve", resolved)
    return HeadlessLaneTurn(
        SimpleNamespace(),  # type: ignore[arg-type]
        world.sm,
        world.settings,
        SimpleNamespace(agent_name=None),  # type: ignore[arg-type]
        {},  # type: ignore[arg-type]
        membership_resolver=resolver,
    )


async def _work(world: World) -> runs.StepWork:
    """Step `a` of a new run, as its executor would get it."""
    claimed = await world.claim((await world.run()).id, "a")
    seen: list[runs.StepWork] = []

    async def capture(work: runs.StepWork) -> engine.Outcome:
        seen.append(work)
        return engine.Failed("fatal", "captured")

    await runs.execute(world.sm, world.settings, {"agent": capture}, claimed, clock=lambda: NOW)
    return seen[0]


async def _drive(world: World, lane: HeadlessLaneTurn) -> uuid.UUID:
    """One run to its end through the loops; a minute passes per clock read."""
    ticks = iter(range(1000))

    def clock() -> datetime:
        return NOW + timedelta(minutes=next(ticks))

    executor = AgentStepExecutor(world.sm, world.settings, lane, clock=clock)
    loop = WorkflowEngine(world.sm, world.settings, {"agent": executor}, owner="w1", clock=clock)
    run_id = (await world.run()).id
    for _ in range(10):
        await run_once(loop)
        if (await world.get(run_id)).status != "running":
            break
    return run_id


async def _notices(world: World) -> list[DeliveryRow]:
    async with world.sm() as s:
        rows = await workflow_deliveries.unsent_routine_posts(s, world.workflow_id)
    return [r for r in rows if r.kind == "notice"]


async def test_lapsed_role_fails_run(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A routine paused and DMed a card; the step fails the run, told once per streak."""
    world = await _world(db_session, policy=GATED)
    lane = _lane(world, monkeypatch, _member())
    first, second = await _drive(world, lane), await _drive(world, lane)
    for run_id in (first, second):
        got = await world.get(run_id)
        assert (got.status, got.failure_reason) == ("failed", "run_as_lapsed"), got
    async with world.sm() as s:
        flow = await workflows.get_workflow(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id
        )
    assert flow is not None and flow.status == "active", "the workflow is not paused"
    [notice] = await _notices(world)
    assert notice.target["channel_id"] == HOME, "one notice, at home"


async def test_membership_wait_retries(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A routine waited quietly for the next fire; the step retries, then fails loudly."""
    world = await _world(db_session, policy=GATED)
    run_id = await _drive(world, _lane(world, monkeypatch, _member(known=False)))
    step = (await world.steps(run_id))["a"]
    async with world.sm() as s:
        tries = await workflow_runs.list_attempts(s, step.id)
    assert [a.error_class for a in tries] == ["retryable", "retryable"], "retried before failing"
    assert (await world.get(run_id)).failure_reason == "membership_unavailable"


async def test_dm_routine_runs_headless_private(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shape B: no place, personal memory, and a DM to post its result to."""
    nodes = {
        "a": {"kind": "agent", "agent": "helper", "instructions": "go", "memory": "personal",
              "next": "p"},
        "p": {"kind": "post", "to": "run_as", "message": "${{ steps.a.summary }}", "next": "f"},
        "f": {"kind": "finish"},
    }  # fmt: skip
    world = await _world(db_session, nodes)
    monkeypatch.setattr(lane_turn, "open_discord_dm", AsyncMock(return_value="777"))
    place = await _lane(world, monkeypatch, None).admit(await _work(world))
    assert not isinstance(place, engine.Failed), place
    assert (place.where, place.may_post, place.dm_channel) == (None, False, "777")
    assert place.admitted is not None and place.admitted.channel_id is None, "admitted unplaced"


async def _allow(_call: ToolCall) -> ToolConfirmationResult:
    return ToolConfirmationResult(allow=True)


async def test_step_cannot_edit_routines() -> None:
    attempt = uuid.uuid4()
    node = AgentNode.model_validate({"kind": "agent", "instructions": "x", "writes": "allow"})
    decide = step_decider(
        node, attempt_id=attempt, policy=ToolSafetyPolicy(), writes="allow", propose=None
    )(PolicyApproval(decide=_allow)).decide
    tools = sorted(t for t in AUTHORING_TOOLS if "routine" in t)
    assert len(tools) == 5, tools
    for tool in tools:
        call = ToolCall(
            tool_use_id="t", server_name=DAIMON_SERVER_NAME, tool_name="call_tool",
            input={"name": tool, "arguments": {}},
        )  # fmt: skip
        assert not (await decide(call)).allow, tool


async def test_brief_carries_place_facts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a routine's controls said: where it works, whether it posts, the quiet line."""
    nodes = {"a": STEP["a"] | {"can_finish_early": True}, "f": STEP["f"]}
    world = await _world(db_session, nodes)
    work = await _work(world)
    place = await _lane(world, monkeypatch, None).admit(work)
    assert not isinstance(place, engine.Failed), place
    node = work.node
    assert isinstance(node, AgentNode)
    brief = render_brief(
        work.spec, "a", node, run_number=1, attempt=1, attempt_id=uuid.uuid4(), context={},
        inputs={}, untrusted_source=None, place=place,
    )  # fmt: skip
    for line in (
        f"You work in {HOME}. You may post there yourself when the instructions say so;",
        "then Daimon does not post your result again.",
        QUIET_RUN_SENTENCE.strip(),
        "A plain final reply also completes this step",
    ):
        assert line in brief, line


class _Store:
    """What a routine fire recorded of its refusal."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    async def refuse(self, _s: object, reason: str, *, tell: bool = True, **_kw: object) -> None:
        self.seen.append(("refuse" if tell else "wait", reason))

    async def pause_for_lapsed_role(self, _s: object, *, error: str) -> bool:
        self.seen.append(("lapsed", error))
        return False


#: Per refusal reason: the policy, the owner's membership, and what else is off.
REFUSALS: dict[str, tuple[TenantAccessPolicy | None, CachedMembershipResolver | None, str]] = {
    "tenant_archived": (None, None, "archived"),
    "owner_lacks_agent_role": (GATED, _member(), ""),
    "membership_unavailable": (GATED, _member(known=False), ""),
    "runs_elsewhere": (TenantAccessPolicy(agent_rules={"helper": AgentRule(runs_in=("999",))}), None, ""),
    "bot_access_unavailable": (None, None, "bot"),
}  # fmt: skip


@pytest.mark.parametrize("reason", list(REFUSALS))
async def test_a_routine_and_an_agent_step_at_its_place_get_one_verdict(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Each refusal is the routine's: a wait retries, a lapsed role fails as `run_as_lapsed`."""
    policy, resolver, off = REFUSALS[reason]
    world = await _world(db_session, policy=policy)
    work = await _work(world)
    async with world.sm.begin() as s:
        row = await create_routine(
            s, tenant_id=world.tenant_id, created_by_user_id=OWNER, agent_id="agent_a",
            agent_name="helper", cron_expr="0 9 * * *", timezone_="UTC", trigger_message="go",
            destination_kind="channel", destination_id=HOME, channel_id=HOME,
        )  # fmt: skip
    if off == "archived":
        await tenants.set_provision_status(world.sm, tenant_id=world.tenant_id, archive=True)
    if off == "bot":
        monkeypatch.setattr(admit, "_bot_locked_out", AsyncMock(return_value=True))
    store = _Store()
    deps = FireDeps(
        client=SimpleNamespace(),  # type: ignore[arg-type]
        sm=world.sm, settings=world.settings,
        deployment_default=SimpleNamespace(agent_name=None),  # type: ignore[arg-type]
        resolver_cache={},  # type: ignore[arg-type]
        membership_resolver=resolver, turn_deps=lambda: None,  # type: ignore[arg-type, return-value]
        send_dm=None, dm_open=AsyncMock(return_value=True), owner_granted=AsyncMock(return_value=True),
    )  # fmt: skip
    await fire_turn(routine_subject(row), cast("FireStore", store), deps)
    [(kind, said)] = store.seen
    expected = {
        "refuse": engine.Failed("gate", "", said),
        "wait": engine.Failed("retryable", said, said),
        "lapsed": engine.Failed("gate", "", "run_as_lapsed"),
    }[kind]
    got = await _lane(world, monkeypatch, resolver).admit(work)
    assert isinstance(got, engine.Failed), got
    assert (got.error_class, got.reason) == (expected.error_class, expected.reason), (kind, got)
    assert said == reason or kind == "lapsed" and reason == "owner_lacks_agent_role", said


@dataclass(frozen=True)
class _Admission:
    """The fields a provider step's turn reads of its admission."""

    agent: Any
    backend_revision: ConfigRevision
    channel_skills: tuple[Any, ...] = ()
    backend: Any = None


class _Outputs:
    """A provider session's workspace holding one file."""

    session_id = "prov_ses"

    async def list_outputs(self) -> list[Any]:
        meta = SimpleNamespace(
            id="o1", filename="a.csv", mime_type="text/csv", size_bytes=3, max_read_bytes=None
        )
        return [meta]

    async def read_output(self, _id: str) -> bytes:
        return b"a,b"


async def _provider_step(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    *,
    resume: bool = False,
    refused: AdmissionDenied | None = None,
) -> tuple[Any, dict[str, Any], dict[str, Any], runs.StepWork]:
    """Step `a` admitted at a place on `provider`, then one turn of it: what the turn returned,
    what its admission and its run were given, and the step."""
    world = await _world(db_session, {"a": STEP["a"] | {"model": "m-step"}, "f": STEP["f"]})
    monkeypatch.setattr(admit, "place_provider", AsyncMock(return_value=provider))
    lane = _lane(world, monkeypatch, None)
    work = await _work(world)
    place = await lane.admit(work)
    assert not isinstance(place, engine.Failed) and place.admitted is not None, place
    assert place.admitted.native == provider, "the place's provider runs the step"
    revision = ConfigRevision.create(
        channel_ref(world.tenant_id, "discord", HOME), 1, backend_selection(provider)
    )
    agent = SimpleNamespace(id="agent_a", name="helper", metadata={})
    admitted, ran, sessions = {}, {}, []

    async def admit_turn(_deps: object, row: Any, **kw: Any) -> _Admission:
        admitted.update(kw, row=row)
        if refused is not None:
            raise refused
        return _Admission(agent, revision)

    async def run_turn(_deps: object, admission: _Admission, **kw: Any) -> ProviderRoutineRun:
        ran.update(kw, admission=admission, execution=current_intake_execution.get())
        await kw["on_session"]("prov_ses")
        return ProviderRoutineRun("Done.", TurnState(content=[]), cast("Any", _Outputs()))

    async def on_session(sid: str) -> None:
        sessions.append(sid)

    deps = cast("Any", SimpleNamespace(sessionmaker=world.sm))
    provider_turn = ProviderLaneTurn(deps, admit=admit_turn, run=run_turn)
    lane = HeadlessLaneTurn(lane.client, lane.sm, lane.settings, lane.deployment_default,
                            lane.resolver_cache, provider=provider_turn)  # fmt: skip
    step = StepTurn(
        tools=cast("Any", None), decide=lambda p: p, workflow_run_id=work.run.id,
        workflow_step_id=work.step.id, on_session=on_session, resume=resume, skills=("s1",),  # type: ignore[arg-type]
    )  # fmt: skip
    reply = await lane(
        work, place=place, message="brief", session_id=None, step=step, policy=ToolSafetyPolicy()
    )
    ran_turn = not resume and refused is None
    assert sessions == (["prov_ses"] if ran_turn else []), "the attempt and lane keep the session"
    return reply, admitted, ran, work


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_provider_step_runs_in_its_lane_on_the_places_provider(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Admitted as a provider routine at the step's place, run in its lane's own scope with
    the step's model and skills, under the run's credential; its files are staged."""
    reply, admitted, ran, work = await _provider_step(db_session, monkeypatch, provider)
    assert (admitted["owner"], admitted["refusal"]) == (OWNER, STEP_SHELL_NEEDS_GRANT), admitted
    assert admitted["row"].channel_id == HOME, "billed to and configured by the step's place"
    node = work.node
    assert isinstance(node, AgentNode)
    lane = lane_key(work.spec, work.step.node_id, node, work.step.visit)
    assert ran["scope_id"] == workflow_scope_thread(work.run.id, lane), ran
    assert (ran["prompt"], ran["reuse_existing"]) == ("brief", False), ran
    execution = ran["execution"]
    assert isinstance(execution, WorkflowExecution) and execution == (
        work.run.id,
        lane_grant(work.run.id, lane),
    ), "the turn's Daimon credential carries the run and lane: its step's tools and decider"
    admission = ran["admission"]
    assert admission.backend_revision.model == "m-step", "the step's model runs"
    assert admission.backend_revision.backend == provider
    assert admission.channel_skills == ("s1",), "the step's pinned skills ride along"
    assert not isinstance(reply, engine.Failed) and reply.text == "Done.", reply
    assert reply.files is not None and [(f["filename"], f["staged"]) for f in reply.files] == [
        ("a.csv", True)
    ], "the file is staged while the provider session is live: `steps.<id>.files`"


async def test_provider_step_does_not_reattach_after_a_lost_worker(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberate difference: Managed Agents reattaches to a lost worker's turn; a provider
    turn can't be, so the attempt fails `retryable` (the executor's `_drive`) and the next
    attempt starts afresh."""
    with pytest.raises(RuntimeError, match=lane_turn.NO_REATTACH):
        await _provider_step(db_session, monkeypatch, "gemini", resume=True)


async def test_provider_step_refused_at_admission_fails_at_its_gate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenAI's shell without the step's built-ins grant: the step fails with the fix."""
    refused = AdmissionDenied(reason="backend_unsupported", detail=STEP_SHELL_NEEDS_GRANT)
    reply, *_ = await _provider_step(db_session, monkeypatch, "openai", refused=refused)
    assert isinstance(reply, engine.Failed), reply
    assert (reply.error_class, reply.reason) == ("gate", "backend_unsupported"), reply
    assert "Turn on built-in tools" in reply.message, reply
