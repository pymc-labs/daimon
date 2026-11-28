"""A moved routine posts what its routine fire posted, where it posted it (shapes A-D)."""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
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
from daimon.core import routine_files, routine_provider
from daimon.core.channel_backend import channel_ref
from daimon.core.delivery import job_payload
from daimon.core.direct_messages import DmRoutineReply
from daimon.core.fire import admit
from daimon.core.fire.run import FireDeps, fire_turn
from daimon.core.fire.store import FireResult, FireStore
from daimon.core.fire.subject import routine_subject
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.provider_routine import ProviderRoutineRun
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import identity, tenants, workflow_deliveries, workflows
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.core.workflows import agent_step, convert
from daimon.core.workflows.access import Who
from daimon.core.workflows.agent_step import AgentStepExecutor, session_files
from daimon.core.workflows.place import workflow_subject
from daimon.core.workflows.post_step import PostStepExecutor
from daimon.core.workflows.service import Env
from daimon.core.workflows.spec import AgentNode, parse_spec
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.workflows import NOW, World, agents_with, make_settings
from mux.contracts.config import ConfigRevision
from mux.profiles import backend_selection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

OWNER, HOME, THREAD = "111", "555", "666"
#: Per shape: the routine's destination and channel, and where its agent posts itself.
SHAPES: dict[str, tuple[str | None, str | None, str | None]] = {
    "A channel": ("channel", HOME, HOME),
    "A thread": ("thread", THREAD, HOME),
    "A thread elsewhere": ("thread", THREAD, "777"),
    "A legacy channel": ("channel", "777", HOME),
    "B dm": ("dm", OWNER, None),
    "C channel only": (None, None, HOME),
    "D nothing": (None, None, None),
}
REPLIES = {
    "text": ("Weekly summary: all green.", False, False),
    "no post": ("NO_POST: nothing new", False, False),
    "files": ("Report attached.", True, False),
    "agent posted": ("Posted it.", False, True),
    "agent posted files": ("Posted it.", True, True),
}


async def _member(*_a: object) -> Membership:
    return Membership(is_member=True)


MEMBER = CachedMembershipResolver(_member)
#: A routine with only a channel ran unplaced; its moved step works at home, so that channel's
#: policy, hold and origin stamp apply to it, as to anyone working there (stricter).
STRICTER = {"C channel only"}
FILE = SimpleNamespace(id="f1", filename="a.csv", mime_type="text/csv", size_bytes=3)
Posted = tuple[str, str, tuple[str, ...]] | None
#: The Managed Agents path, then a provider fake: the routine fire and the step run on it.
PROVIDERS = [None, "gemini"]
#: Each fake turn's usage stamps, routine fire first.
USAGE: list[dict[str, Any]] = []


@dataclass(frozen=True)
class _Admission:
    """A provider admission, as a fire and a step's turn read it."""

    agent: Any
    backend_revision: ConfigRevision
    channel_skills: tuple[Any, ...] = ()
    backend: Any = None
    budget_channel_id: str | None = None


class _Outputs:
    """A provider session's workspace: `FILE` when the turn made files."""

    session_id = "ses_p"

    def __init__(self, files: bool) -> None:
        self.files = files

    async def list_outputs(self) -> list[Any]:
        return [SimpleNamespace(**vars(FILE), max_read_bytes=None)] if self.files else []

    async def read_output(self, _id: str) -> bytes:
        return b"a,b"


def _provider(row: Any, reply: str, posts_to: str | None, files: bool) -> dict[str, Any]:
    """The provider fake: admits on Gemini and runs one turn that replies `reply`."""
    revision = ConfigRevision.create(
        channel_ref(row.tenant_id, "discord", HOME), 1, backend_selection("gemini")
    )
    agent = SimpleNamespace(id="agent_a", name="daimon", metadata={})

    async def admit_turn(*_a: object, **_kw: object) -> _Admission:
        return _Admission(agent, revision)

    async def run_turn(*_a: object, **kw: Any) -> ProviderRoutineRun:
        if (on_session := kw.get("on_session")) is not None:
            await on_session("ses_p")
        return ProviderRoutineRun(reply, _state(posts_to), cast("Any", _Outputs(files)))

    return {"admit": admit_turn, "run": run_turn}


def _files(files: Any) -> tuple[str, ...]:
    """A post's files: a staged file by name, its handle being new on each staging."""
    return tuple(f.filename if f.staged else f.file_id for f in files)


def _state(posts_to: str | None) -> TurnState:
    if posts_to is None:
        return TurnState(content=[])
    block = ToolUseBlock(
        kind="tool_use", id="tu_post", type="agent.mcp_tool_use", name="send_message",
        input={"channel_id": posts_to, "content": "done"}, mcp_server_name="daimon-mcp",
        status="complete",
    )  # fmt: skip
    return TurnState(content=[block])


class _Store:
    """A routine fire's result, kept instead of written."""

    def __init__(self) -> None:
        self.result: FireResult | None = None

    def execution(self, _lifetime_s: float) -> contextlib.AbstractAsyncContextManager[None]:
        return contextlib.nullcontext()

    async def sandbox(self, *_a: object, **_kw: object) -> None:
        return None

    async def finish(self, result: FireResult) -> None:
        self.result = result

    async def refuse(self, _s: object, reason: str, **_kw: object) -> None:
        raise AssertionError(f"the fire was refused: {reason}")

    async def fail(self, _s: object, *, error: str, **_kw: object) -> bool:
        raise AssertionError(f"the fire failed: {error}")


async def _routine_posted(
    sm: async_sessionmaker[AsyncSession],
    settings: Any,
    row: Any,
    reply: str,
    posts_to: str | None,
    native: dict[str, Any] | None = None,
) -> Posted:
    state = _state(posts_to)

    async def turn(**kw: Any) -> str:
        USAGE.append(kw["usage_record_factory"]("ses_r", "m").keywords)
        kw["on_session"]("ses_r")
        kw["on_state"](state)
        return reply

    async def dm_turn(*_a: object, **_kw: object) -> DmRoutineReply:
        if native is None:
            return DmRoutineReply(reply, "ses_r", "agent_a", on_managed_agents=True)
        run: ProviderRoutineRun = await native["run"]()
        return DmRoutineReply(
            reply, "ses_p", "agent_a", on_managed_agents=False, outputs=run.outputs
        )

    agent = SimpleNamespace(name="daimon", metadata={}, mcp_servers=[], tools=[])
    deps = FireDeps(
        client=SimpleNamespace(),  # type: ignore[arg-type]
        sm=sm, settings=settings,
        deployment_default=DeploymentDefault(),
        resolver_cache={},  # type: ignore[arg-type]
        membership_resolver=MEMBER,
        turn_deps=lambda: SimpleNamespace(membership_resolver=MEMBER),  # type: ignore[arg-type, return-value]
        send_dm=None, dm_open=AsyncMock(return_value=True), owner_granted=AsyncMock(return_value=True),
        run_turn=turn, run_dm_routine=dm_turn, resolve_agent=AsyncMock(return_value="agent_a"),
        resolve_environment=AsyncMock(return_value="env_1"),
        retrieve_agent=AsyncMock(return_value=agent), retrieve_agent_cached=AsyncMock(return_value=agent),
        open_discord_dm=AsyncMock(return_value="777"),
    )  # fmt: skip
    if native is not None:
        deps = replace(
            deps, admit_provider_routine=native["admit"], run_provider_routine=native["run"]
        )
    store = _Store()
    await fire_turn(routine_subject(row), cast("FireStore", store), deps)
    result = store.result
    assert result is not None, "the fire finished"
    payload = routine_files.decode_payload(result.payload)
    if result.delivery != "pending" or not (payload.text or payload.files):
        return None
    place = "dm" if row.destination_kind == "dm" else row.destination_id
    return place, payload.text, _files(payload.files)


async def _workflow_posted(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
    posts_to: str | None,
    native: dict[str, Any] | None = None,
) -> Posted:
    async def turn(*, step: Any, session_id: str | None, **kw: Any) -> str:
        USAGE.append(kw["usage_record_factory"]("ses_w", "m").keywords)
        await step.on_session(session_id or "ses_w")
        kw["on_state"](_state(posts_to))
        return reply

    async def resolved(*_a: object, **_kw: object) -> object:
        return SimpleNamespace(environment_name=None)

    monkeypatch.setattr(lane_turn, "run_turn", turn)
    monkeypatch.setattr(lane_turn, "resolve_agent", AsyncMock(return_value="agent_a"))
    monkeypatch.setattr(lane_turn, "resolve_environment", AsyncMock(return_value="env_1"))
    monkeypatch.setattr(lane_turn, "resolve", resolved)
    monkeypatch.setattr(lane_turn, "open_discord_dm", AsyncMock(return_value="777"))
    client = SimpleNamespace()
    lane = HeadlessLaneTurn(
        client,  # type: ignore[arg-type]
        world.sm, world.settings,
        DeploymentDefault(),
        {},  # type: ignore[arg-type]
        membership_resolver=MEMBER,
        provider=None if native is None else ProviderLaneTurn(
            cast("Any", SimpleNamespace(sessionmaker=world.sm)), **native
        ),
    )  # fmt: skip
    ticks = iter(range(1000))

    def clock() -> datetime:
        return NOW + timedelta(seconds=next(ticks))

    executor = AgentStepExecutor(
        world.sm, world.settings, lane, clock=clock,
        collect_files=lambda sid: session_files(client, sid),  # type: ignore[arg-type]
    )  # fmt: skip
    loop = WorkflowEngine(
        world.sm,
        world.settings,
        {"agent": executor, "post": PostStepExecutor(world.sm)},
        owner="w1",
        clock=clock,
    )
    run_id = (await world.run()).id
    for _ in range(10):
        await run_once(loop)
        if (await world.get(run_id)).status != "running":
            break
    steps = {k: (r.status, r.error_message) for k, r in (await world.steps(run_id)).items()}
    assert all(s != "failed" for s, _ in steps.values()), steps
    async with world.sm() as s:
        rows = await workflow_deliveries.unsent_routine_posts(s, world.workflow_id)
    rows = [r for r in rows if r.kind == "post"]
    if not rows:
        return None
    [row] = rows
    target, payload = row.target, job_payload(row)
    place = "dm" if target["kind"] == "dm" else target.get("thread_id") or target["channel_id"]
    return place, payload.text, _files(payload.files)


async def _routine(
    db_session: AsyncSession, kind: str | None, destination: str | None, channel: str | None
) -> tuple[RoutineRow, Env]:
    """A Discord routine of `OWNER`'s, and what moves it."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild-1")
    account = await make_account(db_session, tenant=tenant)
    await tenants.set_funding_mode(db_session, tenant_id=tenant.id, funding_mode="operator_funded")
    await identity.create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id=OWNER,
        account_id=account.id,
    )  # fmt: skip
    row = await create_routine(
        db_session, tenant_id=tenant.id, created_by_user_id=OWNER, agent_id="agent_a",
        agent_name="daimon", cron_expr="0 9 * * *", timezone_="UTC", trigger_message="go",
        destination_kind=kind, destination_id=destination, channel_id=channel,  # type: ignore[arg-type]
        source_provenance_known=True, made_in_channel_id=channel, source_channel_id=channel,
    )  # fmt: skip
    await db_session.commit()
    sm = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    who = Who(tenant.id, account.id, admin=True)
    return row, Env(sm, make_settings(), who, NOW, agents_with({}))


async def _both(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    reply: str,
    provider: str | None = None,
) -> tuple[Posted, Posted, World, uuid.UUID]:
    """What the routine's fire posted, then what the workflow it moved to posted, both on
    `provider` (Managed Agents for None)."""
    USAGE.clear()
    kind, destination, channel = SHAPES[shape]
    text, files, posts_itself = REPLIES[reply]

    async def listed(*_a: object, **_kw: object) -> dict[str, object]:
        return {"f1": FILE} if files else {}

    monkeypatch.setattr(routine_files, "poll_output_files", listed)
    monkeypatch.setattr(agent_step, "poll_output_files", listed)
    row, env = await _routine(db_session, kind, destination, channel)
    sm, settings = env.sm, env.settings
    posts_to = (destination if kind in ("channel", "thread") else HOME) if posts_itself else None
    native = None
    if provider is not None:
        on = AsyncMock(return_value=provider)
        monkeypatch.setattr(admit, "place_provider", on)
        monkeypatch.setattr(routine_provider, "place_provider", on)
        native = _provider(row, text, posts_to, files)
    fired = await _routine_posted(sm, settings, row, text, posts_to, native)

    flow = await convert.from_routine(env, row.id)
    world = World(sm, settings, env.who.tenant_id, env.who.account_id, flow)
    moved = await _workflow_posted(world, monkeypatch, text, posts_to, native)
    return fired, moved, world, row.id


@pytest.mark.parametrize("provider", PROVIDERS, ids=["managed-agents", "gemini"])
@pytest.mark.parametrize("reply", list(REPLIES))
@pytest.mark.parametrize("shape", list(SHAPES))
async def test_a_moved_routine_posts_what_its_fire_posted(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    reply: str,
    provider: str | None,
) -> None:
    fired, moved, *_ = await _both(db_session, monkeypatch, shape, reply, provider)
    kind, destination, _ = SHAPES[shape]
    posts_to = (
        (destination if kind in ("channel", "thread") else HOME) if REPLIES[reply][2] else None
    )
    assert moved == fired, f"{shape}, {reply}"
    alone = posts_to == destination and not REPLIES[reply][1]
    assert (fired is None) == (kind is None or reply == "no post" or alone), fired


@pytest.mark.parametrize("shape", ["A channel", "A thread", "C channel only", "D nothing"])
async def test_usage_by_run(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Billed as the routine was: its owner, budget channel and agent; keyed by the run, and
    joined to the routine's rows by `converted_from.routine_id`."""
    _, _, world, routine_id = await _both(db_session, monkeypatch, shape, "text")
    fired, moved = USAGE
    same = ("tenant_id", "platform_user_id", "channel_id", "agent_id")
    assert {k: moved[k] for k in same} == {k: fired[k] for k in same}, (fired, moved)
    async with world.sm() as s:
        flow = await workflows.get_workflow(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id
        )
    run = await world.get(moved["workflow_run_id"])
    assert run.workflow_id == world.workflow_id and "workflow_run_id" not in fired
    assert flow is not None and flow.converted_from is not None
    assert flow.converted_from.get("routine_id") == str(routine_id)


@pytest.mark.parametrize("shape", [s for s in SHAPES if SHAPES[s][2] is not None])
async def test_a_moved_routine_is_admitted_where_its_fire_was(
    db_session: AsyncSession, shape: str
) -> None:
    """Its step's admission facts are the routine fire's: the place, its kind and channel."""
    row, env = await _routine(db_session, *SHAPES[shape])
    flow_id = await convert.from_routine(env, row.id)
    async with env.sm() as s:
        flow = await workflows.get_workflow(s, tenant_id=row.tenant_id, workflow_id=flow_id)
        assert flow is not None and flow.live_version is not None
        live = await workflows.get_version(
            s, tenant_id=row.tenant_id, workflow_id=flow_id, number=flow.live_version
        )
    assert live is not None
    node = parse_spec(live.spec).nodes["run"]
    assert isinstance(node, AgentNode)
    step = workflow_subject(
        flow, node, home=flow.home_channel_id, run_as=OWNER, agent_name="daimon",
        agent_id="agent_a", platform="discord",
    )  # fmt: skip
    fired = routine_subject(row)
    if shape in STRICTER:
        assert fired.destination_kind is None and (step.destination_kind, step.destination_id) == (
            "channel",
            HOME,
        )
        return
    facts = ("destination_kind", "destination_id", "channel_id", "created_by_user_id")
    assert [getattr(step, f) for f in facts] == [getattr(fired, f) for f in facts], shape
