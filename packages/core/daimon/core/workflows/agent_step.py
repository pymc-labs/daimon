"""The Agent step executor: one step turn in its lane's session, settled from the recorded result.

Flow: pick the lane (a busy one gets a lane of its own), continue its session or start one,
save the session on the attempt before the brief is sent, drive the turn through `LaneTurn`
(the scheduler's headless runner), then settle from what `complete_step`, `fail_step` or
`finish_early` recorded. No result earns one nudge, after preflight passes again; still
none fails `no_result`. A question (`ask_human`) parks the step until a person answers;
the answer is the session's next message. A lost worker's attempt keeps its session and
reattaches to the turn in flight. A failed turn replaces its lane, so the retry starts fresh
with the brief.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Literal, Protocol, cast

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_managed_agents_custom_skill import BetaManagedAgentsCustomSkill
from daimon.core.capability_tools import WORKFLOW_AUTHORING_TOOLS
from daimon.core.channel_backend import backend_refusal_causes
from daimon.core.checkpoint_prompt import HANDOFF_FILENAME_PREFIX
from daimon.core.config import Settings
from daimon.core.headless_runner import StepTurn
from daimon.core.output_delivery import MAX_BYTES_CEILING, poll_output_files
from daimon.core.routine_delivery import split_run_text
from daimon.core.routine_files import MAX_FILES, RoutineFile
from daimon.core.stores import workflow_proposals, workflow_runs
from daimon.core.stores.workflow_run_events import NewEvent, append_events
from daimon.core.stores.workflow_waits import Wait
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    PROXY_TOOL_NAME,
    SEARCH_TOOL_NAME,
    ToolCall,
    ToolSafetyPolicy,
    unwrap_daimon_call,
)
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState, daimon_tool_arguments
from daimon.core.turn.unattended_capabilities import (
    BUILTIN_TOOL_NAMES,
    ServerTool,
    StepTools,
)
from daimon.core.workflows import engine, step_tools, waits
from daimon.core.workflows.action_step import (
    ToolClient,
    created_so_far,
    exposed,
    review_proposals,
    reviewed,
    send_proposals,
    step_scopes,
)
from daimon.core.workflows.brief import NUDGE, render_brief, step_inputs
from daimon.core.workflows.expressions import ExpressionError
from daimon.core.workflows.lanes import FRESH, agent_of, lane_key, lane_skills
from daimon.core.workflows.place import StepPlace
from daimon.core.workflows.preflight import Refusal, check
from daimon.core.workflows.runs import ENGINE, Park, Sessions, StepWork, key_of
from daimon.core.workflows.spec import AgentNode, WriteScope
from daimon.core.workflows.unattended import (
    CREATED,
    Call,
    created_ids,
    created_paths,
    decide_unattended_call,
)
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

Writes = Literal["refuse", "propose", "allow", "policy"]
READ_BUILTINS: Final = frozenset({"read", "glob", "grep", "web_fetch", "web_search"})
#: Daimon tools a step never calls: no step builds more automation.
AUTHORING_TOOLS: Final = WORKFLOW_AUTHORING_TOOLS | {
    "create_routine",
    "update_routine",
    "delete_routine",
    "set_routine_sandbox_tools",
    "run_routine",
}
OWN_TOOLS: Final = frozenset(
    {PROXY_TOOL_NAME, SEARCH_TOOL_NAME, "update_plan", *step_tools.STEP_TOOLS}
)
_DAIMON: Final = frozenset({DAIMON_SERVER_NAME})
log = structlog.get_logger(__name__)

#: A session's output files as Files API refs; the scheduler binds the client.
CollectFiles = Callable[[str], Awaitable[list[dict[str, JsonValue]]]]


async def session_files(client: AsyncAnthropic, session_id: str) -> list[dict[str, JsonValue]]:
    """The files a step's session made, as refs `steps.<id>.files` and asks attach.

    Same listing as a routine's files; a failed listing keeps the step's result.
    """
    try:
        listed = await poll_output_files(client, session_id=session_id, sleep=asyncio.sleep)
    except anthropic.APIError as error:
        log.warning("workflow.step_files_failed", causes=backend_refusal_causes(error))
        return []
    return [
        RoutineFile(
            file_id=meta.id,
            filename=meta.filename,
            mime_type=meta.mime_type,
            size_bytes=meta.size_bytes,
        ).model_dump()
        for meta in listed.values()
        if not meta.filename.startswith(HANDOFF_FILENAME_PREFIX)
        and 0 < meta.size_bytes <= MAX_BYTES_CEILING
    ][:MAX_FILES]


def _earlier_file_ids(context: Mapping[str, JsonValue]) -> set[str]:
    """Files earlier steps already hold: a shared lane's session lists them again."""
    steps = context.get("steps")
    ids: set[str] = set()
    for view in (steps if isinstance(steps, dict) else {}).values():
        files = view.get("files") if isinstance(view, dict) else None
        for ref in files if isinstance(files, list) else []:
            if isinstance(ref, dict) and isinstance(ref.get("file_id"), str):
                ids.add(str(ref["file_id"]))
    return ids


@dataclass(frozen=True)
class LaneReply:
    """How one turn ended: its final reply, and where the agent posted by itself."""

    text: str = ""
    posted: frozenset[str] = frozenset()


class LaneTurn(Protocol):
    """Admit a step at its place, then drive one turn of it in `session_id` (None: a new
    session), sending `message`."""

    async def admit(self, work: StepWork) -> StepPlace | engine.Failed: ...

    async def __call__(
        self,
        work: StepWork,
        *,
        place: StepPlace,
        message: str,
        session_id: str | None,
        step: StepTurn,
        policy: ToolSafetyPolicy,
    ) -> LaneReply: ...


def replied(node: AgentNode, text: str) -> engine.Succeeded | None:
    """A plain final reply settles a step that recorded nothing: a `NO_POST` line finishes
    it early where it may, and a step with no output fields takes the reply as its summary."""
    if not text.strip():
        return None
    run_text = split_run_text(text)
    if node.can_finish_early and not run_text.post:
        return engine.Succeeded(summary=run_text.record, early=True)
    if node.output:
        return None
    return engine.Succeeded(summary=text[: step_tools.SUMMARY_MAX])


#: The M8 seam: buffer a third-party write as a proposal and answer the paused call.
#: Records a write as a proposal; gets the call and why it needs review.
Propose = Callable[[ToolCall, str | None], Awaitable[ToolConfirmationResult]]


def step_policy(
    policy: ToolSafetyPolicy, writes: Writes, scopes: Sequence[WriteScope] = ()
) -> ToolSafetyPolicy:
    """The tool policy a step runs under: `allow` lets third-party writes through (rules still
    deny); `policy` keeps the operator's; `refuse` and `propose` let none through unattended
    but its scoped tools, whose arguments `decide_unattended_call` checks first."""
    if writes == "policy":
        return policy
    allowed = ("*",) if writes == "allow" else tuple(dict.fromkeys(s.tool for s in scopes))
    return policy.model_copy(update={"unattended_writes": allowed})


def writes_of(work: StepWork) -> Writes:
    """A test run never writes; otherwise the node's `writes`, `refuse` by default."""
    assert isinstance(work.node, AgentNode)
    return "refuse" if work.run.mode == "test" else work.node.writes or "refuse"


def tools_of(node: AgentNode) -> StepTools:
    builtins = {"read": READ_BUILTINS, "all": BUILTIN_TOOL_NAMES}.get(node.builtins or "none")
    listed = None
    if isinstance(node.tools, list):
        listed = frozenset(ServerTool(*key.split("/", 1)) for key in node.tools if "/" in key)
    return StepTools(builtins or frozenset(), listed, OWN_TOOLS)


def pinned_skills(work: StepWork, node: AgentNode) -> list[BetaManagedAgentsCustomSkill]:
    """The lane's skills (`lane_skills`) at the versions its version (or test run) pinned."""
    pins = work.catalog.skill_pins if work.catalog is not None else {}
    return [
        BetaManagedAgentsCustomSkill(type="custom", skill_id=p.id, version=p.version)
        for p in (pins.get(name) for name in lane_skills(work.spec, work.step.node_id, node))
        if p is not None
    ]


def _key(work: StepWork) -> str:
    return key_of(work.step)


def made_in(
    state: TurnState, paths: Mapping[str, Collection[str]]
) -> list[tuple[str, str, list[str]]]:
    """(call id, `server/tool`, ids) for each finished call whose result a `created_by` rule
    reads, from the result's JSON text."""
    made: list[tuple[str, str, list[str]]] = []
    for block in state.content:
        if isinstance(block, TextBlock) or block.status != "complete" or block.is_error:
            continue
        tool = f"{block.mcp_server_name}/{_tool_name(block)}"
        if block.mcp_server_name is None or tool not in paths:
            continue
        ids: list[str] = []
        for item in block.result_content or []:
            if item.type != "text":
                continue
            try:
                value: JsonValue = json.loads(item.text)
            except ValueError:
                continue
            ids += [i for i in created_ids(paths[tool], value) if i not in ids]
        if ids:
            made.append((block.id, tool, ids))
    return made


def _deny(message: str) -> ToolConfirmationResult:
    return ToolConfirmationResult(allow=False, deny_message=f"{message} The tool call did not run.")


def step_decider(
    node: AgentNode,
    *,
    attempt_id: uuid.UUID,
    policy: ToolSafetyPolicy,
    writes: Writes,
    propose: Propose | None,
    exposed: bool = False,
    test: bool = False,
    scopes: Sequence[WriteScope] = (),
    paths: Mapping[str, Collection[str]] | None = None,
    created: Mapping[str, Collection[str]] | None = None,
) -> Callable[[PolicyApproval], PolicyApproval]:
    """Wrap the unattended decider with the step's grant, its `tools` list and its `writes`;
    every other call, Daimon's own included, goes through `decide_unattended_call` (`exposed`:
    untrusted data can steer this step in this run); Daimon's own step tools and plan stay open.
    `scopes` are the step's granted `writes_to`; their `created_by` ids are `created` (earlier
    steps) plus what this turn's calls returned at `paths`."""
    listed = {k for k in node.tools} if isinstance(node.tools, list) else None
    scoped = {s.tool for s in scopes}
    made: dict[str, set[str]] = {k: set(v) for k, v in (created or {}).items()}

    def observe(state: TurnState) -> None:
        for _, tool, ids in made_in(state, paths or {}):
            made.setdefault(tool, set()).update(ids)

    def wrap(base: PolicyApproval) -> PolicyApproval:
        async def decide(call: ToolCall) -> ToolConfirmationResult:
            inner = unwrap_daimon_call(call, _DAIMON)
            own = inner.server_name == DAIMON_SERVER_NAME
            if own:
                if inner.tool_name in step_tools.STEP_TOOLS:
                    if inner.input.get("step") != str(attempt_id):
                        return _deny(f'Refused {inner.tool_name}: pass step="{attempt_id}".')
                    return ToolConfirmationResult(allow=True)
                if inner.tool_name in AUTHORING_TOOLS:
                    return _deny(
                        f"Refused {inner.tool_name}: a workflow step cannot change automations."
                    )
                if (
                    listed is not None
                    and inner.tool_name not in OWN_TOOLS
                    and inner.key not in listed
                ):
                    return _deny(f"Refused {inner.key}: this step may only use its listed tools.")
            if inner.server_name is not None and not (own and inner.tool_name in OWN_TOOLS):
                decision = decide_unattended_call(
                    policy,
                    Call(
                        inner.server_name,
                        inner.tool_name,
                        writes,
                        test=test,
                        exposed=exposed,
                        args=cast("dict[str, JsonValue]", dict(inner.input)),
                        scopes=scopes,
                        created=made,
                    ),
                )
                # Daimon's server is trusted by the base decider, and a scoped tool is allowed
                # by it, so neither can refuse a `writes: refuse` step for us; other refusals
                # of that kind are the base's, which knows a read by its listing.
                if decision.verdict == "refuse" and (
                    own or inner.key in scoped or decision.reason != "writes_refused"
                ):
                    return _deny(f"Refused {inner.key}: {decision.message}")
                if decision.verdict == "propose":
                    if own:
                        return _deny(
                            f"Refused {inner.key}: Daimon's own writes cannot wait for review."
                        )
                    if propose is None:
                        return _deny(f"Refused {inner.key}: proposed writes are not available.")
                    return await propose(inner, decision.reason)
            return await base.decide(call)

        return PolicyApproval(decide=decide, observe=observe if paths else None)

    return wrap


def _tool_name(block: ToolUseBlock) -> str:
    inner = block.input.get("name") if block.name == PROXY_TOOL_NAME else None
    return inner if isinstance(inner, str) else block.name


def _plan_text(arguments: Mapping[str, object]) -> str:
    """`update_plan`'s checklist as the step's note."""
    steps = arguments.get("steps")
    marks = {"completed": "[x]", "in_progress": "[>]"}
    lines: list[str] = []
    for row in cast("list[object]", steps) if isinstance(steps, list) else []:
        if isinstance(row, dict):
            item = cast("dict[str, object]", row)
            lines.append(f"{marks.get(str(item.get('status')), '[ ]')} {item.get('step')}")
    return "\n".join(lines)


@dataclass
class _Activity:
    """Activity lines from the turn as it renders: each tool call once, each plan as a note."""

    sm: Sessions
    work: StepWork
    clock: Callable[[], datetime]
    seen: set[str]
    #: Where `created_by` rules find ids in a result, by `server/tool` (live runs).
    paths: Mapping[str, Collection[str]]

    async def __call__(self, state: TurnState) -> None:
        lines: list[tuple[str, Literal["tool", "note", "message"], str, str | None]] = []
        for block in state.content:
            if isinstance(block, TextBlock):
                continue
            if block.id not in self.seen:
                self.seen.add(block.id)
                name = _tool_name(block)
                lines.append(
                    (block.id, "tool", name, f"{block.mcp_server_name or 'sandbox'}/{name}")
                )
            plan = daimon_tool_arguments(block, "update_plan")
            if plan is not None and (key := f"{block.id}:plan") not in self.seen:
                self.seen.add(key)
                if text := _plan_text(plan):
                    lines.append((key, "note", text, None))
        made = [m for m in made_in(state, self.paths) if f"{m[0]}:made" not in self.seen]
        if not lines and not made:
            return
        self.seen.update(f"{m[0]}:made" for m in made)
        work = self.work
        async with self.sm.begin() as s:
            for _, kind, text, tool in lines:
                await workflow_runs.add_activity(
                    s,
                    tenant_id=work.run.tenant_id,
                    step_id=work.step.id,
                    kind=kind,
                    text=text,
                    tool=tool,
                    now=self.clock(),
                )
            # Kept at call time, so a later step's `created_by` rule can name them.
            events = [NewEvent(CREATED, _key(work), {"tool": t, "ids": i}) for _, t, i in made]
            await append_events(s, run_id=work.run.id, events=events, actor=ENGINE, at=self.clock())


@dataclass(frozen=True)
class AgentStepExecutor:
    sm: Sessions
    settings: Settings
    turn: LaneTurn
    #: Records proposed writes and sends them once approved; without it proposals are refused.
    tools: ToolClient | None = None
    collect_files: CollectFiles | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, work: StepWork) -> engine.Outcome | Park:
        node = work.node
        assert isinstance(node, AgentNode), "the agent executor runs agent nodes"
        if (held := await reviewed(self.sm, work)) is not None:
            return await send_proposals(
                self.sm, self.settings, self.tools, work, held, self.clock()
            )
        key = _key(work)
        async with self.sm.begin() as s:
            attempt = await workflow_runs.running_attempt(s, work.step.id)
            assert attempt is not None, "a claimed step has a running attempt"
            # `runs.claim` gave a busy lane's step a lane of its own.
            lane = work.step.lane or lane_key(work.spec, work.step.node_id, node, work.step.visit)
            session_id = attempt.session_id or await workflow_runs.get_lane(
                s, run_id=work.run.id, lane=lane
            )
        try:
            inputs = step_inputs(node, work.context, self.clock())
        except ExpressionError as err:
            return engine.Failed("fatal", str(err))
        place = await self.turn.admit(work)
        if isinstance(place, engine.Failed):
            return place
        brief = render_brief(
            work.spec,
            work.step.node_id,
            node,
            run_number=work.run.number,
            attempt=work.attempt,
            attempt_id=attempt.id,
            context=work.context,
            inputs=inputs,
            untrusted_source=work.run.trigger_kind if work.run.untrusted_inputs else None,
            place=place,
        )
        resume = attempt.session_id is not None
        asked = await self._asked(work, attempt.id) if resume else None
        if asked is not None and asked.status == "waiting":
            return Park("question", wait=asked.id)
        message = brief if asked is None else await self._answered(work, key, attempt.id, asked)

        async def on_session(sid: str) -> None:
            async with self.sm.begin() as s:
                await workflow_runs.set_attempt_session(
                    s, attempt_id=attempt.id, session_id=sid, now=self.clock()
                )
                await workflow_runs.put_lane(
                    s,
                    tenant_id=work.run.tenant_id,
                    run_id=work.run.id,
                    lane=lane,
                    agent_id=agent_of(work.spec, node) or "",
                    session_id=sid,
                )
                if not resume:
                    data: dict[str, JsonValue] = {
                        "attempt": str(attempt.id),
                        "session": sid,
                        "brief": brief,
                    }
                    await self._event(s, work, key, step_tools.BRIEF_SENT, data)

        writes = writes_of(work)
        scopes = await step_scopes(self.sm, self.settings, work, node)
        policy = step_policy(self.settings.tool_safety, writes, scopes)
        paths = created_paths(work.spec) if work.run.mode == "live" else {}
        step = StepTurn(
            tools=tools_of(node),
            decide=step_decider(
                node,
                attempt_id=attempt.id,
                policy=policy,
                writes=writes,
                propose=self._proposer(work),
                exposed=exposed(work),
                test=work.run.mode == "test",
                scopes=scopes,
                paths=paths,
                created=await created_so_far(self.sm, work.run.id) if scopes else None,
            ),
            workflow_run_id=work.run.id,
            workflow_step_id=work.step.id,
            on_session=on_session,
            resume=resume and asked is None,
            on_render=_Activity(self.sm, work, self.clock, set(), paths),
            skills=pinned_skills(work, node),
        )
        result: step_tools.Recorded | None = None
        posted: set[str] = set()
        reply = LaneReply()
        for _ in range(waits.MAX_QUESTIONS + 1):
            reply = await self._drive(work, lane, place, message, session_id, step, policy)
            if isinstance(reply, engine.Failed):
                return reply
            posted |= reply.posted
            result = await self._recorded(work, attempt.id)
            asked = None if result else await self._asked(work, attempt.id)
            if asked is None:
                break
            if asked.status == "waiting":
                return Park("question", wait=asked.id)
            message = await self._answered(work, key, attempt.id, asked)
            async with self.sm() as s:
                session_id = await workflow_runs.get_lane(s, run_id=work.run.id, lane=lane)
            step = replace(step, resume=False)
        outcome = step_tools.outcome(result) if result is not None else replied(node, reply.text)
        if outcome is None:
            refused = await self._recheck(work)
            if refused is not None:
                return refused
            async with self.sm.begin() as s:
                await self._event(s, work, key, "step.nudged", {"attempt": str(attempt.id)})
                lane_session = await workflow_runs.get_lane(s, run_id=work.run.id, lane=lane)
            nudge = replace(step, resume=False)
            reply = await self._drive(work, lane, place, NUDGE, lane_session, nudge, policy)
            if isinstance(reply, engine.Failed):
                return reply
            posted |= reply.posted
            result = await self._recorded(work, attempt.id)
            outcome = (
                step_tools.outcome(result) if result is not None else replied(node, reply.text)
            )
        if isinstance(outcome, engine.Succeeded):
            outcome = replace(outcome, posted=tuple(sorted(posted)))
        if isinstance(outcome, engine.Succeeded) and self.collect_files is not None:
            async with self.sm() as s:
                made_in = await workflow_runs.get_lane(s, run_id=work.run.id, lane=lane)
            if made_in is not None:
                earlier = _earlier_file_ids(work.context)
                files = [
                    f for f in await self.collect_files(made_in) if f.get("file_id") not in earlier
                ]
                outcome = replace(outcome, files=tuple(files))
        if node.session == FRESH:
            await self._close_lane(work, lane)
        if outcome is None:
            return engine.Failed(
                "contract",
                "The agent stopped without calling complete_step or fail_step",
                reason="no_result",
            )
        if isinstance(outcome, engine.Succeeded):
            review = await review_proposals(
                self.sm, self.settings, self.tools, work, outcome, self.clock()
            )
            if review is not None:
                return review
        return outcome

    def _proposer(self, work: StepWork) -> Propose | None:
        tools = self.tools
        if tools is None:
            return None

        async def propose(call: ToolCall, reason: str | None) -> ToolConfirmationResult:
            url = await tools.server_url(work, call.server_name or "")
            if url is None:
                return _deny(f"Refused {call.key}: the agent does not connect it.")
            async with self.sm.begin() as s:
                added = await workflow_proposals.add_proposal(
                    s,
                    tenant_id=work.run.tenant_id,
                    step_id=work.step.id,
                    server_url=url,
                    tool=call.key,
                    arguments=dict(call.input),
                    reason=reason,
                )
            if added is None:
                return _deny(f"Refused {call.key}: this step has proposed all it may.")
            return ToolConfirmationResult(
                allow=False,
                deny_message=f"Recorded as proposal {added[0]}. Daimon sends it after review; "
                "carry on as if it will be sent.",
            )

        return propose

    async def _drive(
        self,
        work: StepWork,
        lane: str,
        place: StepPlace,
        message: str,
        session_id: str | None,
        step: StepTurn,
        policy: ToolSafetyPolicy,
    ) -> engine.Failed | LaneReply:
        try:
            return await self.turn(
                work,
                place=place,
                message=message,
                session_id=session_id,
                step=step,
                policy=policy,
            )
        except Exception as err:  # a stopped turn's pending calls are never answered
            await self._close_lane(work, lane)
            return engine.Failed("retryable", f"{type(err).__name__}: {err}"[:500])

    async def _recheck(self, work: StepWork) -> engine.Failed | None:
        """Preflight again before the nudge spends, against what the run has spent so far."""
        async with self.sm() as s:
            run = await workflow_runs.get_run(s, work.run.id)
        if work.place is None or run is None:
            return None
        refusal = await check(
            self.sm,
            self.settings,
            work.place,
            now=self.clock(),
            budget_usd=run.budget_usd,
            cost_usd=run.cost_usd,
        )
        if isinstance(refusal, Refusal):
            return engine.Failed("gate", refusal.message, reason=refusal.reason)
        return None

    async def _asked(self, work: StepWork, attempt_id: uuid.UUID) -> Wait | None:
        async with self.sm() as s:
            return await waits.pending_question(
                s, run_id=work.run.id, step=work.step, attempt_id=attempt_id
            )

    async def _answered(self, work: StepWork, key: str, attempt_id: uuid.UUID, asked: Wait) -> str:
        """The answer as the session's next message, marked sent so it goes once."""
        async with self.sm.begin() as s:
            data: dict[str, JsonValue] = {"attempt": str(attempt_id), "answer": str(asked.id)}
            await self._event(s, work, key, step_tools.BRIEF_SENT, data)
        return waits.answer_message(asked, attempt_id)

    async def _recorded(self, work: StepWork, attempt_id: uuid.UUID) -> step_tools.Recorded | None:
        async with self.sm() as s:
            return await step_tools.recorded(
                s, run_id=work.run.id, key=_key(work), attempt_id=attempt_id
            )

    async def _close_lane(self, work: StepWork, lane: str) -> None:
        assert isinstance(work.node, AgentNode)
        async with self.sm.begin() as s:
            await workflow_runs.put_lane(
                s,
                tenant_id=work.run.tenant_id,
                run_id=work.run.id,
                lane=lane,
                agent_id=agent_of(work.spec, work.node) or "",
                session_id=None,
            )

    async def _event(
        self, s: AsyncSession, work: StepWork, key: str, type: str, data: dict[str, JsonValue]
    ) -> None:
        events = [NewEvent(type, key, data)]
        await append_events(s, run_id=work.run.id, events=events, actor=ENGINE, at=self.clock())
