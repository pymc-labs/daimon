"""A routine fired as a routine and its translation fired as a workflow's routine step run the
same turn and make the same posts, usage, failure handling and refusals; a routine never fires
both ways; and a move writes the rows the workflow stores would.

Both paths share `fire_turn`, so only the stores and the input builders can differ: each shape
seeds the same routine in two workspaces, fires one through `run_one_tick` and moves the other
with `move_routine` before firing its run through `RoutineStep`, with the same recording fakes.
"""

from __future__ import annotations

import asyncio
import functools
import re
import uuid
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.scheduler.main import _fire_deps  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.scheduler.workflows import RoutineStep
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.direct_messages import DmRoutineReply
from daimon.core.fire.delivery import DeliveryJob, routine_job
from daimon.core.fire.run import Dispatch, FireDeps, fire_turn
from daimon.core.fire.subject import Subject, routine_subject
from daimon.core.lead_intake import IntakeConfig
from daimon.core.lead_intake_execution import current_intake_execution
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.ops_catalog import reason_label
from daimon.core.ops_events import drain_ops_events
from daimon.core.scheduler import RoutineFireStore, drain_owner_notices, run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import feature_values, ops_events, routines, workflow_runs, workflows
from daimon.core.stores import workflow_deliveries as deliveries
from daimon.core.stores import workflow_triggers as triggers_store
from daimon.core.stores.domain import SANDBOX_KEY, FeatureTarget, RoutineRow, SandboxPin, pin_json
from daimon.core.stores.lead_intake import IntakeStore
from daimon.core.stores.shared_channels import mark_channel_shared
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.core.workflows import convert, engine, runs, triggers
from daimon.core.workflows.access import Who
from daimon.core.workflows.delivery import result_job
from daimon.core.workflows.fire_store import WorkflowFireStore
from daimon.core.workflows.routine_translation import NOTE, STEP, Refusal, ids
from daimon.core.workflows.runs import StepWork
from daimon.core.workflows.service import Env
from daimon.testing.builders import new_routine
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
)
from daimon.testing.workflows import agents_with, make_settings
from mux.contracts.config import BackendConfig
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.usefixtures("instant_routine_file_settle")

OWNER, CHANNEL, REPLY = "1001", "555", "The day in short"
PIN = SandboxPin("agent_a", 3, "env_1", datetime(2026, 10, 1, tzinfo=UTC))
#: The run binding: per-workspace ids, the callbacks, and the routine or run a turn is tagged with.
RUN_BINDING = frozenset(
    {
        "on_state",
        "on_session",
        "usage_record_factory",
        "tenant_id",
        "agent_uuid",
        "account_id",
        "routine_id",
        "routine_step_run_id",
        "turn_deps",
    }
)
#: A moved post's payload fields only the way back reads: the routine's notice, version and fire
#: time, and the run number a moved post has none of.
CARRIED = frozenset({"routine_notice", "version", "fired_at", "run_number"})
#: A job's ids, times and recorded source, which name the routine or the run.
JOB_REFS = frozenset(
    {
        "id",
        "tenant_id",
        "workflow_run_id",
        "workflow",
        "source",
        "delivery_id",
        "last_fired_at",
        "revision",
    }
)


@dataclass(frozen=True)
class Shape:
    destination: tuple[str, str] | None = ("channel", CHANNEL)
    owner: str | None = OWNER
    #: Where the agent posts with its own messaging tool.
    posts_to: str | None = None
    raises: bool = False
    held: bool = False
    sandbox: bool = False
    #: The workspace's `lead_intake` value, for a routine bound to lead intake.
    intake: str | None = None
    gemini: bool = False
    name: str | None = "Digest"
    #: What both paths refuse with, in the routine's last error and the step's error.
    refusal: str | None = None
    reply: str = REPLY


SHAPES = {
    "channel": Shape(),
    "unnamed": Shape(name=None),
    "thread": Shape(destination=("thread", "777")),
    "dm": Shape(destination=("dm", OWNER)),
    "no_destination": Shape(destination=None),
    "no_post": Shape(reply="NO_POST: no new PRs"),
    "no_post_dm": Shape(destination=("dm", OWNER), reply="NO_POST"),
    "sandboxed": Shape(sandbox=True),
    "lead_intake": Shape(destination=("dm", OWNER), intake="on"),
    "lead_intake_off": Shape(
        destination=("dm", OWNER), intake="off", refusal="lead_intake_turned_off"
    ),
    "held": Shape(held=True),
    "autonomous": Shape(destination=None, posts_to="999"),
    "agent_posted": Shape(posts_to=CHANNEL),
    "failure": Shape(raises=True),
}


@dataclass
class Seen:
    """What one fire did, as both paths can be compared: every id is scrubbed."""

    turns: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    grants: list[bool] = field(default_factory=list[bool])
    usage: list[str] = field(default_factory=list[str])
    jobs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    notices: list[tuple[str, bool]] = field(default_factory=list[tuple[str, bool]])
    ops: list[tuple[str, str | None, str, int]] = field(
        default_factory=list[tuple[str, str | None, str, int]]
    )
    error: str | None = None


@dataclass
class Place:
    """One workspace's copy of the shape's routine."""

    sm: async_sessionmaker[AsyncSession]
    row: RoutineRow
    account_id: uuid.UUID
    seen: Seen = field(default_factory=Seen)

    def scrub(self, value: object) -> str:
        text = value if isinstance(value, str) else repr(value)
        for raw, name in (
            (self.row.tenant_id, "<tenant>"),
            (self.row.id, "<id>"),
            (ids(self.row.id)["workflow"], "<id>"),
            (self.account_id, "<account>"),
        ):
            text = text.replace(str(raw), name)
        return text


@pytest.fixture
def committing(
    db_committing_engine: AsyncEngine, db_session: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    """Fires and moves on their own connections, as in production; `db_session` wipes first."""
    return async_sessionmaker(bind=db_committing_engine, expire_on_commit=False)


def _client() -> AsyncAnthropic:
    """Offline: the only call a fire here makes is the empty file listing after a DM turn."""

    def files(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": False})

    transport = httpx.MockTransport(files)
    return AsyncAnthropic(api_key="offline", http_client=httpx.AsyncClient(transport=transport))


async def _member(_tenant: object, _guild: object, _user: object) -> Membership:
    return Membership(is_member=True)


async def _yes(*_args: object, **_kwargs: object) -> bool:
    return True


async def _agent_a(*_args: object, **_kwargs: object) -> str:
    return "agent_a"


async def _place(sm: async_sessionmaker[AsyncSession], shape: Shape, now: datetime) -> Place:
    """A workspace with the shape's routine due now, its owner and the state around it."""
    async with sm.begin() as s:
        tenant = await make_tenant(s, workspace_id=str(uuid.uuid4().int)[:18])
        account = await make_account(s, tenant=tenant)
        await make_ledger_entry(s, tenant=tenant, delta_usd=Decimal("10"))
        if shape.owner is not None:
            await make_platform_principal(
                s, platform="discord", external_id=shape.owner, tenant=tenant, account=account
            )
        kind, target = shape.destination or (None, None)
        row = await new_routine(
            s,
            tenant,
            created_by_user_id=shape.owner,
            agent_id="agent_a",
            name=shape.name,
            cron_expr="0 * * * *",
            trigger_message="Sum up the day",
            next_fire_at=now - timedelta(minutes=1),
            destination_kind=kind,
            destination_id=target,
            channel_id=CHANNEL,
            source_provenance_known=True,
            source_channel_id=CHANNEL,
        )
        if shape.held:
            await mark_channel_shared(
                s, tenant_id=tenant.id, platform="discord", channel_id=CHANNEL, source="platform"
            )
        if shape.gemini:
            backend = BackendConfig(
                backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
            )
            await set_channel_backend(s, channel_ref(tenant.id, "discord", CHANNEL), backend)
        if shape.sandbox:
            await feature_values.set_feature_value(
                s,
                tenant_id=tenant.id,
                target=FeatureTarget("routine", routine_id=row.id),
                key=SANDBOX_KEY,
                value="on",
                set_by=None,
                surface="cli",
                pin=pin_json(PIN),
            )
    if shape.intake == "off":
        async with sm.begin() as s:
            await feature_values.set_feature_value(
                s,
                tenant_id=tenant.id,
                target=FeatureTarget("workspace"),
                key="lead_intake",
                value="off",
                set_by=None,
                surface="cli",
            )
    if shape.intake is not None:
        config = IntakeConfig(
            agent_id=uuid.uuid4(),
            guild_id="1",
            channel_id=CHANNEL,
            source_connection_id="gmail-1",
            gmail_mailbox="owner@example.test",
            crm_connection_id="hubspot-1",
            portal_id="3",
            routine_id=row.id,
            reconciliation_start=now - timedelta(days=1),
        )
        await IntakeStore(sm, make_fernet(), tenant.id).configure(config, {})
    return Place(sm, row, account.id)


def _deps(
    place: Place, shape: Shape, client: AsyncAnthropic, settings: Any, resolver: Any
) -> FireDeps:
    """The scheduler's fire deps with recording fakes for the turn and every Anthropic call."""
    seen = place.seen

    def record(kwargs: dict[str, Any]) -> None:
        seen.grants.append(current_intake_execution.get() is not None)
        kept = {k: v for k, v in kwargs.items() if k not in RUN_BINDING}
        if (sandbox := kept.get("sandbox")) is not None:
            kept["sandbox"] = sandbox.pin  # its drift callback is per fire
        seen.turns.append({k: place.scrub(v) for k, v in sorted(kept.items())})
        if (factory := kwargs.get("usage_record_factory")) is not None:
            bound = cast("functools.partial[object]", factory("sesn_1", "claude-sonnet-4-6"))
            tags = {"sessionmaker", "tenant_id", "workflow_run_id"}
            seen.usage.append(
                place.scrub({k: v for k, v in sorted(bound.keywords.items()) if k not in tags})
            )
        if shape.raises:
            raise RuntimeError("boom")

    async def run_turn(**kwargs: Any) -> str:
        record(kwargs)
        blocks: list[ToolUseBlock] = []
        if shape.posts_to is not None:
            blocks.append(
                ToolUseBlock(
                    kind="tool_use",
                    id="tu_post",
                    type="agent.mcp_tool_use",
                    name="send_message",
                    input={"channel_id": shape.posts_to, "content": "done"},
                    mcp_server_name="daimon-mcp",
                    status="complete",
                )
            )
        kwargs["on_state"](TurnState(content=list(blocks)))
        return shape.reply

    async def run_dm_routine(_turn_deps: object, **kwargs: Any) -> DmRoutineReply:
        record(kwargs)
        return DmRoutineReply(shape.reply, "sesn_dm", "agent_a")

    base = _fire_deps(
        client=client,
        sm=place.sm,
        settings=settings,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        membership_resolver=resolver,
        send_dm=None,
    )()
    turn_deps = cast("TurnDeps", SimpleNamespace(membership_resolver=resolver))
    return replace(
        base,
        run_turn=run_turn,
        run_dm_routine=run_dm_routine,
        resolve_agent=_agent_a,
        resolve_environment=_agent_a,
        dm_open=_yes,
        owner_granted=_yes,
        turn_deps=lambda: turn_deps,
    )


async def _settle(place: Place, jobs: list[DeliveryJob]) -> None:
    """The fire's ops events, posts (each still postable) and nothing else."""
    seen = place.seen
    for job in jobs:
        assert await job.still_current() is None, f"the claimed post still goes out: {job}"
        kept = {f.name: getattr(job, f.name) for f in fields(job) if f.compare}
        seen.jobs.append({k: place.scrub(v) for k, v in kept.items() if k not in JOB_REFS})
    await drain_ops_events()
    async with place.sm() as s:
        groups = await ops_events.list_issue_groups(
            s, tenant_id=place.row.tenant_id, since=datetime.now(UTC) - timedelta(days=1)
        )
    seen.ops = sorted((g.latest.code, g.latest.reason, g.latest.severity, g.events) for g in groups)


async def _tick(place: Place, deps: FireDeps, now: datetime) -> list[tuple[str, bool]]:
    """Fire the due routine through the scheduler tick; the owner notices it sent."""
    told: list[tuple[str, bool]] = []

    async def notify(_row: RoutineRow, reason: str, channel_told: bool) -> None:
        told.append(((reason_label(reason) or "Unknown").lower(), channel_told))

    async def fire(row: RoutineRow) -> None:
        await fire_turn(routine_subject(row), RoutineFireStore(place.sm, row, notify), deps)

    await run_one_tick(
        now=now,
        sm=place.sm,
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=1,
        dispatch_timeout_s=60,
        wait_for_completion=True,
        notify_owner=notify,
    )
    await drain_owner_notices(5)
    return told


async def _as_routine(place: Place, deps: FireDeps, now: datetime) -> None:
    told = await _tick(place, deps, now)
    row = place.row
    async with place.sm.begin() as s:
        after = await routines.get_routine(s, row.id, tenant_id=row.tenant_id)
        claimed = await routines.claim_routine_deliveries(
            s, platform="discord", owner="d1", now=datetime.now(UTC), lease=timedelta(minutes=5)
        )
    assert after is not None
    place.seen.error, place.seen.notices = after.last_error, told
    await _settle(place, [await routine_job(place.sm, r, platform="discord") for r in claimed])


async def _run_step(
    place: Place, deps: FireDeps, settings: Any, now: datetime, flow: uuid.UUID
) -> bool:
    """Fire the workflow's slot and run its routine step; False when no run started."""
    sm = place.sm
    await triggers.fire_schedules(sm, now=now)
    await triggers.start_from_events(sm, settings, now=now)
    async with sm() as s:
        active = await workflow_runs.active_run_ids(
            s, workflow_id=flow, concurrency_key=None, mode="live"
        )
        steps = [] if not active else await workflow_runs.list_steps(s, active[0])
    if not active:
        return False
    step_id = next(r.id for r in steps if r.node_id == STEP)
    claimed = await runs.claim(sm, step_id=step_id, owner="w1", lease=timedelta(minutes=5), now=now)
    assert claimed is not None, "the routine step is claimed"
    stores: list[WorkflowFireStore] = []

    async def turn(subject: Subject, store: WorkflowFireStore) -> None:
        stores.append(store)
        await fire_turn(subject, store, deps)

    step = RoutineStep(sm, Dispatch(sm, asyncio.Semaphore(1), 60), _agent_a, turn)
    ended: list[engine.Outcome] = []

    async def run_step(work: StepWork) -> engine.Outcome:
        ended.append(await step(work))
        return ended[-1]

    await runs.execute(sm, settings, {"routine": run_step}, claimed, clock=lambda: now)
    outcome = stores[0].outcome if stores else None
    place.seen.error = outcome.error if outcome else None
    # A gate's skip ends the step as succeeded with its reason, where a routine records it.
    if isinstance(done := ended[0], engine.Succeeded) and isinstance(done.output, dict):
        place.seen.error = cast("str | None", done.output.get("skipped", place.seen.error))
    return True


async def _as_workflow(place: Place, deps: FireDeps, settings: Any, now: datetime) -> bool:
    sm = place.sm
    if not await _run_step(place, deps, settings, now, ids(place.row.id)["workflow"]):
        return False
    async with sm.begin() as s:
        posts = await deliveries.claim_deliveries(
            s, platform="discord", owner="d1", now=datetime.now(UTC), lease=timedelta(minutes=5)
        )
    for post in posts:
        if post.kind == "notice":
            found = re.search(r"stopped: (.+?)\.( Its channel was told\.)?$", post.payload["text"])
            assert found is not None, f"an owner notice: {post.payload}"
            place.seen.notices.append((found[1], found[2] is not None))
    jobs = [await result_job(sm, p, platform="discord") for p in posts if p.kind == "result"]
    await _settle(place, [j for j in jobs if j is not None])
    return True


async def _both(sm: async_sessionmaker[AsyncSession], shape: Shape) -> tuple[Place, Place, bool]:
    now = datetime.now(UTC)
    settings = make_settings()
    resolver = CachedMembershipResolver(_member)
    client = _client()
    routine, flow = await _place(sm, shape, now), await _place(sm, shape, now)
    async with sm.begin() as s:  # before the tick, which would claim it too
        await convert.move_routine(s, flow.row.tenant_id, flow.row.id, now)
    await _as_routine(routine, _deps(routine, shape, client, settings, resolver), now)
    ran = await _as_workflow(flow, _deps(flow, shape, client, settings, resolver), settings, now)
    await client.close()
    return routine, flow, ran


@pytest.mark.parametrize("name", list(SHAPES))
async def test_a_routine_step_fires_as_its_routine_did(
    committing: async_sessionmaker[AsyncSession], name: str
) -> None:
    shape = SHAPES[name]
    routine, flow, ran = await _both(committing, shape)
    one, other = routine.seen, flow.seen
    assert ran, "the moved routine's slot starts a run"
    assert bool(one.turns) == (shape.refusal is None), f"a turn runs unless refused: {one}"
    assert shape.refusal is None or shape.refusal in (one.error or ""), f"refused: {one.error}"
    assert other.turns == one.turns, "the same turn inputs, apart from the run binding"
    assert other.grants == one.grants, "the same execution grant during the turn"
    assert other.usage == one.usage, "the same usage binding, apart from the run tag"
    assert other.jobs == one.jobs, "the same posts, apart from their source and ids"
    assert other.notices == one.notices, "the same owner notice"
    assert other.ops == one.ops, "the same ops events"
    assert other.error == one.error, "the same failure or refusal"


async def test_an_ownerless_routine_moves_paused_and_never_fires(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """Deliberate difference: an ownerless routine refuses each slot with `no_owner`; its
    workflow is paused, so it starts no run at all."""
    routine, flow, ran = await _both(committing, Shape(owner=None))
    assert routine.seen.error and "no owner" in routine.seen.error, "the routine refuses"
    assert not ran and not flow.seen.turns, "the paused workflow does not run"
    assert routine.seen.turns == [] and routine.seen.jobs == [], "neither runs a turn or posts"


async def test_on_another_provider_a_routine_runs_where_its_step_refuses(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """Deliberate difference: a routine on Gemini runs as a chat-path turn (`_fire_native`);
    workflows run only on Anthropic, so its step refuses before admission."""
    admitted: list[str] = []

    async def admit(_deps: object, facts: Subject, **_kw: object) -> NoReturn:
        admitted.append(facts.agent_name)
        raise AdmissionDenied(reason="backend_unsupported")

    shape = Shape(gemini=True)
    now = datetime.now(UTC)
    settings, resolver, client = make_settings(), CachedMembershipResolver(_member), _client()
    routine, flow = await _place(committing, shape, now), await _place(committing, shape, now)
    async with committing.begin() as s:
        await convert.move_routine(s, flow.row.tenant_id, flow.row.id, now)
    deps = replace(_deps(routine, shape, client, settings, resolver), admit_provider_routine=admit)
    await _as_routine(routine, deps, now)
    ran = await _as_workflow(flow, _deps(flow, shape, client, settings, resolver), settings, now)
    await client.close()
    assert admitted == [routine.row.agent_name], f"admitted on its provider: {routine.seen.error}"
    assert ran and not flow.seen.turns and not admitted[1:], "the step runs no turn"
    assert "Workflows run only on Anthropic" in (flow.seen.error or ""), flow.seen.error


async def test_a_routine_never_fires_as_both_across_the_move_and_back(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """While moving, the locked row is skipped; once moved only the workflow takes its slot;
    once back only the routine takes the next one."""
    now = datetime.now(UTC)
    place = await _place(committing, Shape(destination=None), now)
    row = place.row
    fired: list[uuid.UUID] = []

    async def fire(claimed: RoutineRow) -> None:
        fired.append(claimed.id)

    async def tick(at: datetime) -> None:
        await run_one_tick(
            now=at,
            sm=committing,
            fire=fire,
            max_age=timedelta(minutes=15),
            max_concurrent_fires=1,
            dispatch_timeout_s=5,
            wait_for_completion=True,
        )

    async with committing.begin() as moving:
        _, move = await convert.plan_move(moving, row.tenant_id, row.id, now)
        await asyncio.wait_for(tick(now), timeout=10)
        assert fired == [], "a routine mid-move is never claimed"
        assert not isinstance(move, Refusal), f"it moves: {move}"
        await convert.apply(moving, row.tenant_id, move)
    await tick(now)
    assert fired == [], "a moved routine is never claimed"
    assert await triggers.fire_schedules(committing, now=now) == 1, "the workflow takes the slot"

    env = Env(
        committing,
        make_settings(),
        Who(row.tenant_id, place.account_id, admin=True),
        now,
        agents_with({"daimon": []}),
    )
    assert await convert.to_routine(env, move.id) == row.id
    await tick(now)
    assert fired == [], "the slot the workflow took is not fired again"
    later = now.replace(minute=1, second=0, microsecond=0) + timedelta(hours=1)
    await tick(later)
    assert fired == [row.id], "the routine takes the next slot"
    assert await triggers.fire_schedules(committing, now=later) == 0, "and the workflow is gone"


async def test_a_move_writes_the_rows_the_workflow_stores_would(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """A moved routine's workflow, version, schedule, sandbox feature, owner notice and pending
    result equal the ones creating and publishing the same workflow and firing its step write."""
    now, settings, shape = datetime.now(UTC), make_settings(), Shape(sandbox=True)
    place = await _place(committing, shape, now)
    row, client = place.row, _client()
    deps = _deps(place, shape, client, settings, CachedMembershipResolver(_member))
    await _tick(place, deps, now)  # its result stays pending
    async with committing.begin() as s:
        await routines.queue_owner_notice(s, row.id, {"title": "Digest", "body": "x", "text": "x"})
        moved = await convert.move_routine(s, row.tenant_id, row.id, now)
    async with committing.begin() as s:
        made = await workflows.get_workflow(s, tenant_id=row.tenant_id, workflow_id=moved.move.id)
        assert made is not None, "the move made the workflow"
        online = await workflows.create_workflow(
            s,
            tenant_id=row.tenant_id,
            owner_account_id=place.account_id,
            name=made.name,
            spec=moved.spec,
            home_channel_id=made.home_channel_id,
            made_in=made.made_in,
            converted_from=made.converted_from,
        )
        await workflows.publish_version(
            s,
            tenant_id=row.tenant_id,
            workflow_id=online.id,
            spec=moved.spec,
            spec_hash=moved.move.spec_hash,
            source="routine",
            published_by=place.account_id,
            note=NOTE,
            now=now,
        )
        await feature_values.set_feature_value(
            s,
            tenant_id=row.tenant_id,
            target=FeatureTarget("workflow", workflow_id=online.id),
            key=SANDBOX_KEY,
            value="on",
            set_by=None,
            surface="cli",
            pin=pin_json(PIN),
        )
        await triggers_store.add_owner_notice(
            s, tenant_id=row.tenant_id, workflow_id=online.id, topic="routine_failed", text="x"
        )

    async def rows(flow: uuid.UUID) -> dict[str, str]:
        async with committing() as s:
            found = await workflows.get_workflow(s, tenant_id=row.tenant_id, workflow_id=flow)
            version = await workflows.get_version(
                s, tenant_id=row.tenant_id, workflow_id=flow, number=1
            )
            schedule = await triggers_store.list_triggers(s, workflow_id=flow)
            values = await feature_values.place_values(
                s,
                tenant_id=row.tenant_id,
                agent_id=None,
                channel_id=None,
                thread_key=None,
                workflow_id=flow,
            )
            posts = sorted(await deliveries.unsent_routine_posts(s, flow), key=lambda p: p.kind)
        assert found is not None and version is not None, "the workflow is published"
        made: dict[str, list[BaseModel]] = {
            "workflow": [found],
            "version": [version],
            "schedule": list(schedule),
            "features": list(values),
            "posts": list(posts),
        }
        times = {"id", "created_at", "updated_at", "draft_updated_at", "published_at", "set_at"}
        # A step's post names its run; a moved one has none, and carries the routine's version
        # and fire time for the way back.
        times |= {"run_id", "step_id", "attempts"}
        # A moved schedule keeps the routine's cursor, so the slot it fired is not fired again.
        times |= {"last_fired_slot", "seeded_after"}
        out: dict[str, str] = {}
        for key, models in made.items():
            dumped = [m.model_dump(exclude=times) for m in models]
            for d in dumped:
                if isinstance(payload := d.get("payload"), dict):
                    kept = cast("dict[str, Any]", payload)
                    d["payload"] = {k: v for k, v in kept.items() if k not in CARRIED}
            out[key] = repr(dumped).replace(str(flow), "<flow>")
        return out

    moved_rows, online_rows = await rows(moved.move.id), await rows(online.id)
    slot = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    assert await _run_step(place, deps, settings, slot, online.id), "the online workflow runs"
    await client.close()
    online_rows["posts"] = (await rows(online.id))["posts"]
    for key, written in online_rows.items():
        assert moved_rows[key] == written, f"a move writes the {key} the stores write"
