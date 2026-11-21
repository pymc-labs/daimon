"""Agent steps against the database, with a scripted agent in place of the lane's MA session."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core import usage_recording
from daimon.core.headless_runner import StepTurn
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores import workflow_deliveries, workflow_runs, workflow_waits
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from daimon.core.workflows import answers, brief, inbox, run_views, runs, step_tools, waits
from daimon.core.workflows.access import Who
from daimon.core.workflows.agent_step import (
    AgentStepExecutor,
    CollectFiles,
    Writes,
    step_decider,
)
from daimon.core.workflows.runs import StepWork
from daimon.core.workflows.service import Env
from daimon.core.workflows.spec import AgentNode, SchemaField, parse_spec
from daimon.testing.workflows import NOW, World, make_world
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

Agent = Callable[["Lane", StepWork, str], Awaitable[None]]


@dataclass
class Lane:
    """A scripted agent: each turn runs the next script against the step tools."""

    world: World
    scripts: list[Agent]
    sessions: list[str | None] = field(default_factory=list[str | None])
    resumes: list[bool] = field(default_factory=list[bool])
    attempt: uuid.UUID | None = None
    step: StepTurn | None = None

    async def __call__(
        self,
        work: StepWork,
        *,
        message: str,
        session_id: str | None,
        step: StepTurn,
        writes: Writes,
    ) -> None:
        self.sessions.append(session_id)
        self.resumes.append(step.resume)
        self.step = step
        sid = session_id or f"ses_{len(self.sessions)}"
        await step.on_session(sid)
        if found := re.search(r'step="([0-9a-f-]{36})"', message):
            self.attempt = uuid.UUID(found.group(1))
        assert work.step.started_at is not None
        await self.bill(sid, f"evt_{len(self.sessions)}", work.step.started_at)
        await self.scripts.pop(0)(self, work, message)

    async def bill(self, sid: str, event_id: str, at: datetime) -> None:
        assert self.step is not None
        await usage_recording.record_turn_usage(
            sessionmaker=self.world.sm,
            tenant_id=self.world.tenant_id,
            platform_user_id=None,
            managed_session_id=sid,
            model_id="claude-opus-4-7",
            event=BetaManagedAgentsSpanModelRequestEndEvent(
                id=event_id,
                is_error=False,
                model_request_start_id="start",
                model_usage=ma_model_usage(input_tokens=1000, output_tokens=100),
                processed_at=at + timedelta(seconds=30),
                type="span.model_request_end",
            ),
            pricing=MODEL_PRICING["claude-opus-4-7"],
            workflow_run_id=self.step.workflow_run_id,
        )

    async def call(self, act: Callable[[AsyncSession, step_tools.Grant], Awaitable[str]]) -> str:
        """What the MCP step tools do, as the run's owner."""
        assert self.attempt is not None, "the brief names the step"
        async with self.world.sm.begin() as s:
            g = await step_tools.grant(
                s,
                attempt_id=self.attempt,
                account_id=self.world.owner,
                tenant_id=self.world.tenant_id,
                lock=True,
            )
            return await act(s, g)


def completes(output: JsonValue, route: str | None = None) -> Agent:
    async def agent(lane: Lane, work: StepWork, message: str) -> None:
        await lane.call(
            lambda s, g: step_tools.complete(
                s, g, output=output, summary="done", route=route, reason=None, now=NOW
            )
        )

    return agent


async def idle(lane: Lane, work: StepWork, message: str) -> None:
    """Ends the turn without a result."""


async def finishes_early(lane: Lane, work: StepWork, message: str) -> None:
    await lane.call(lambda s, g: step_tools.finish_early(s, g, summary="nothing new", now=NOW))


async def drive(
    world: World, run_id: uuid.UUID, lane: Lane, files: CollectFiles | None = None
) -> None:
    """The steps loop by hand: claim each ready agent step and execute it."""
    for i in range(10):
        now = NOW + timedelta(minutes=i)
        executor = AgentStepExecutor(
            world.sm, world.settings, lane, collect_files=files, clock=lambda now=now: now
        )
        ready = [k for k, s in (await world.steps(run_id)).items() if s.status == "ready"]
        if not ready:
            return
        claimed = await world.claim(run_id, ready[0], now)
        await runs.execute(
            world.sm, world.settings, {"agent": executor}, claimed, clock=lambda now=now: now
        )


COLLECT: dict[str, Any] = {
    "collect": {
        "kind": "agent",
        "instructions": "List the risky deals",
        "output": {"risky_count": {"type": "integer"}, "deals": {"type": "array"}},
        "can_finish_early": True,
        "next": "check",
    },
    "check": {
        "kind": "branch",
        "cases": [{"when": "${{ steps.collect.output.risky_count > 0 }}", "to": "write"}],
        "else": "quiet",
    },
    "write": {
        "kind": "agent",
        "instructions": "Write the digest",
        "inputs": {"deals": "${{ steps.collect.output.deals }}"},
        "output": {"digest": {"type": "string"}},
        "next": "done",
    },
    "done": {"kind": "finish", "output": "${{ steps.write.output }}"},
    "quiet": {"kind": "finish"},
}


async def test_agent_then_finish_records_output_cost_and_ledger(db_session: AsyncSession) -> None:
    world = await make_world(
        db_session,
        {
            "a": {
                "kind": "agent",
                "instructions": "Say hi",
                "output": {"greeting": {"type": "string"}},
                "next": "f",
            },
            "f": {"kind": "finish", "output": "${{ steps.a.output }}"},
        },
        "a",
    )
    run = await world.run()
    lane = Lane(world, [completes({"greeting": "hi"})])
    await drive(world, run.id, lane)
    done = await world.get(run.id)
    step = (await world.steps(run.id))["a"]
    assert (done.status, step.output) == ("succeeded", {"greeting": "hi"}), f"{done}"
    assert step.cost_usd == done.cost_usd > 0, f"the turn's cost is on step and run: {step}"
    types = await world.verified(run.id)
    assert {"step.brief_sent", "step.output_recorded", "step.succeeded"} <= set(types), types
    async with world.sm() as s:
        state = run_views.project(await run_views.event_views(s, done))
    [a] = [x for x in state.steps if x.key == "a"]
    assert a.attempts[-1].session, "the step shows the agent session it ran in"


async def test_digest_continues_the_main_lane_through_a_branch(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    lane = Lane(
        world,
        [completes({"risky_count": 1, "deals": ["Acme"]}), completes({"digest": "Acme: call"})],
    )
    await drive(world, run.id, lane)
    done = await world.get(run.id)
    steps = await world.steps(run.id)
    assert (done.status, done.outcome) == ("succeeded", "completed"), f"{done}"
    assert lane.sessions == [None, "ses_1"], f"write continues collect's session: {lane.sessions}"
    assert steps["collect"].cost_usd > 0 and steps["write"].cost_usd > 0, "each step has its cost"
    assert done.cost_usd == steps["collect"].cost_usd + steps["write"].cost_usd, f"{done}"
    await world.verified(run.id)
    async with world.sm() as s:
        state = run_views.project(await run_views.event_views(s, done))
    lanes = {x.key: x.lane for x in state.steps if x.key in ("collect", "write")}
    assert set(lanes.values()) == {steps["collect"].lane}, f"step.started names the lane: {lanes}"
    assert [(x.id, x.session) for x in state.lanes] == [(steps["collect"].lane, "ses_1")], (
        f"the run lists its one lane with the session its first brief opened: {state.lanes}"
    )


async def test_a_steps_files_are_the_new_ones_its_session_made(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    lane = Lane(
        world,
        [completes({"risky_count": 1, "deals": ["Acme"]}), completes({"digest": "Acme: call"})],
    )
    made: list[dict[str, JsonValue]] = []

    async def files(session_id: str) -> list[dict[str, JsonValue]]:
        made.append({"file_id": f"file_{len(made)}", "filename": "chart.png"})
        return list(made)

    await drive(world, run.id, lane, files)
    steps = await world.steps(run.id)
    assert [f["file_id"] for f in steps["collect"].files] == ["file_0"], steps["collect"]
    assert [f["file_id"] for f in steps["write"].files] == ["file_1"], "only the new file"


async def test_finish_early_ends_the_run_with_nothing_to_do(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    await drive(world, run.id, Lane(world, [finishes_early]))
    done = await world.get(run.id)
    assert (done.status, done.outcome) == ("succeeded", "nothing_to_do"), f"{done}"
    assert "write" not in await world.steps(run.id), "the rest of the run never starts"
    await world.verified(run.id)


async def test_a_quiet_finish_is_nothing_to_do(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    await drive(world, run.id, Lane(world, [completes({"risky_count": 0, "deals": []})]))
    done = await world.get(run.id)
    assert (done.status, done.outcome) == ("succeeded", "nothing_to_do"), f"{done}"


async def test_no_result_gets_one_nudge_then_fails(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    lane = Lane(world, [idle, completes({"risky_count": 0, "deals": []})])
    await drive(world, run.id, lane)
    assert (await world.get(run.id)).status == "succeeded", "the nudge turn's result counts"
    assert lane.sessions == [None, "ses_1"], "the nudge goes to the same session"
    assert "step.nudged" in await world.verified(run.id), "the nudge is on the ledger"

    again = await world.run()
    await drive(world, again.id, Lane(world, [idle, idle]))
    failed = await world.get(again.id)
    step = (await world.steps(again.id))["collect"]
    assert (failed.status, step.error_class) == ("failed", "contract"), f"{failed} {step}"


async def test_contract_errors_come_back_exact_and_are_capped(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    replies: list[str] = []

    async def wrong(lane: Lane, work: StepWork, message: str) -> None:
        for _ in range(3):
            replies.append(
                await lane.call(
                    lambda s, g: step_tools.complete(
                        s,
                        g,
                        output={"risky_count": "x"},
                        summary="",
                        route=None,
                        reason=None,
                        now=NOW,
                    )
                )
            )
        try:
            await lane.call(lambda s, g: step_tools.fail(s, g, reason="?", kind="other", now=NOW))
        except step_tools.StepRefused as err:
            replies.append(str(err))

    await drive(world, run.id, Lane(world, [wrong]))
    assert "output.risky_count must be a integer" in replies[0], replies
    assert "output.deals is missing" in replies[0], replies
    assert (await world.get(run.id)).status == "failed", "fail_step still records after rejections"


async def test_a_lost_worker_reattaches_to_its_session(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    claimed = await world.claim(run.id, "collect")
    async with world.sm.begin() as s:
        attempt = await workflow_runs.running_attempt(s, claimed.step.id)
        assert attempt is not None
        await workflow_runs.set_attempt_session(
            s, attempt_id=attempt.id, session_id="ses_x", now=NOW
        )
    await runs.reclaim(
        world.sm, world.settings, step_id=claimed.step.id, now=NOW + timedelta(minutes=10)
    )
    lane = Lane(world, [completes({"risky_count": 0, "deals": []})])
    await drive(world, run.id, lane)
    assert (lane.sessions, lane.resumes) == (["ses_x"], [True]), "the same session, resumed"
    assert (await world.get(run.id)).status == "succeeded", "the reattached turn settles the step"


async def test_the_grant_refuses_other_people_and_finished_steps(db_session: AsyncSession) -> None:
    world = await make_world(db_session, COLLECT, "collect")
    run = await world.run()
    seen: list[str] = []

    async def stranger(lane: Lane, work: StepWork, message: str) -> None:
        assert lane.attempt is not None
        async with world.sm() as s:
            for account in (uuid.uuid4(), world.owner):
                try:
                    await step_tools.grant(
                        s, attempt_id=lane.attempt, account_id=account, tenant_id=world.tenant_id
                    )
                    seen.append("granted")
                except step_tools.StepRefused:
                    seen.append("refused")
        await completes({"risky_count": 0, "deals": []})(lane, work, message)

    lane = Lane(world, [stranger])
    await drive(world, run.id, lane)
    assert seen == ["refused", "granted"], "only the run-as person holds the grant"
    try:
        await lane.call(lambda s, g: step_tools.fail(s, g, reason="late", kind="other", now=NOW))
        late = "granted"
    except step_tools.StepRefused:
        late = "refused"
    assert late == "refused", "a finished step's grant is gone"


async def _allow(call: ToolCall) -> ToolConfirmationResult:
    return ToolConfirmationResult(allow=True)


async def test_the_decider_holds_the_step_to_its_grant_tools_and_writes() -> None:
    attempt = uuid.uuid4()
    node = AgentNode.model_validate(
        {"kind": "agent", "instructions": "x", "tools": ["crm/search"], "writes": "propose"}
    )
    decide = step_decider(
        node, attempt_id=attempt, policy=ToolSafetyPolicy(), writes="propose", propose=None
    )(PolicyApproval(decide=_allow)).decide

    def daimon(tool: str, **args: object) -> ToolCall:
        return ToolCall(
            tool_use_id="t",
            server_name=DAIMON_SERVER_NAME,
            tool_name="call_tool",
            input={"name": tool, "arguments": args},
        )

    cases = {
        "own step": daimon("complete_step", step=str(attempt)),
        "other step": daimon("complete_step", step=str(uuid.uuid4())),
        "authoring": daimon("create_routine"),
        "workflow authoring": daimon("edit_workflow_draft"),
        "workflow run": daimon("run_workflow"),
        "unlisted": daimon("list_channels"),
        "write": ToolCall(tool_use_id="t", server_name="crm", tool_name="create_deal"),
        "read": ToolCall(tool_use_id="t", server_name="crm", tool_name="search"),
    }
    got = {k: (await decide(c)).allow for k, c in cases.items()}
    assert got == {
        "own step": True,
        "other step": False,
        "authoring": False,
        "workflow authoring": False,
        "workflow run": False,
        "unlisted": False,
        "write": False,
        "read": True,
    }, got


def test_the_brief_carries_the_contract_and_untrusted_inputs() -> None:
    spec = parse_spec(
        {"schema": "daimon.workflow/v1", "name": "Digest", "start": "collect", "nodes": COLLECT}
    )
    node = spec.nodes["write"]
    assert isinstance(node, AgentNode)
    attempt = uuid.uuid4()
    text = brief.render_brief(
        spec,
        "write",
        node,
        run_number=3,
        attempt=1,
        attempt_id=attempt,
        context={"steps": {"collect": {"status": "succeeded"}}},
        inputs={"deals": ["<b>Acme</b>"]},
        untrusted_source="webhook",
    )
    assert f'step="{attempt}"' in text, "the brief names the step grant"
    assert "output: {digest: string}" in text, text
    assert "[write] (agent) -> done" in text and "done: collect" in text, text
    assert "<untrusted_content" in text and "<b>" not in text, "inputs are escaped and enveloped"


def test_check_output_fills_defaults_and_names_each_error() -> None:
    fields = {
        "fit": SchemaField(type="string", enum=["strong", "poor"]),
        "n": SchemaField(type="integer", default=0),
        "tags": SchemaField(type="array", items=SchemaField(type="string")),
    }
    filled, errors = brief.check_output(fields, {"fit": "strong", "tags": ["a"]})
    assert (filled, errors) == ({"fit": "strong", "n": 0, "tags": ["a"]}, []), errors
    _, errors = brief.check_output(fields, {"fit": "maybe", "tags": [1], "x": True})
    assert errors == [
        "output.x is not in the contract",
        "output.fit must be one of strong, poor",
        "output.tags[0] must be a string",
    ], errors
    assert brief.check_output(None, {}) == (None, []), "no contract takes no output"


async def test_a_question_parks_the_step_and_the_answer_resumes_its_session(
    db_session: AsyncSession,
) -> None:
    nodes = {
        "a": {
            "kind": "agent",
            "instructions": "Do it",
            "can_ask": True,
            "output": {"pick": {"type": "string"}},
            "next": "f",
        },
        "f": {"kind": "finish", "output": "${{ steps.a.output }}"},
    }
    world = await make_world(db_session, nodes, "a")
    run = await world.run()
    asked: list[str] = []

    async def asks(lane: Lane, work: StepWork, message: str) -> None:
        asked.append(
            await lane.call(lambda s, g: waits.ask(s, g, question="Which?", options=[], now=NOW))
        )

    async def answered(lane: Lane, work: StepWork, message: str) -> None:
        assert "<answer>Blue</answer>" in message, message
        await completes({"pick": "Blue"})(lane, work, message)

    lane = Lane(world, [asks, answered])
    await drive(world, run.id, lane)
    step = (await world.steps(run.id))["a"]
    assert (step.status, step.waiting_reason) == ("waiting", "question"), step
    async with world.sm() as s:
        wait = (await workflow_waits.waits_of_step(s, step.id))[-1]
    who = Who(tenant_id=world.tenant_id, account_id=world.owner)
    answer = waits.Answer(route="answer", values={"text": "Blue"})
    await waits.decide_wait(world.sm, world.settings, who, wait.id, answer, via="web", now=NOW)
    await drive(world, run.id, lane)
    assert (await world.get(run.id)).status == "succeeded", "the step carries on"
    assert lane.sessions == [None, "ses_1"] and lane.resumes == [False, False], lane


async def test_a_question_with_fields_asks_a_choice_and_a_note_and_answers_with_both(
    db_session: AsyncSession,
) -> None:
    """`ask_human` fields reach the inbox form, the chat modal and the agent in their order."""
    nodes = {
        "a": {"kind": "agent", "instructions": "Do it", "can_ask": True, "next": "f"},
        "f": {"kind": "finish"},
    }
    world = await make_world(db_session, nodes, "a")
    run = await world.run()
    fields = [
        waits.AskField(
            name="channel", title="Channel", choices=["#delivery", "#leads"], required=True
        ),
        waits.AskField(name="note", title="Note"),
    ]
    told: list[str] = []

    async def asks(lane: Lane, work: StepWork, message: str) -> None:
        await lane.call(
            lambda s, g: waits.ask(s, g, question="Where?", options=[], fields=fields, now=NOW)
        )

    async def answered(lane: Lane, work: StepWork, message: str) -> None:
        told.append(message)
        await completes(None)(lane, work, message)

    lane = Lane(world, [asks, answered])
    await drive(world, run.id, lane)
    step = (await world.steps(run.id))["a"]
    async with world.sm() as s:
        wait = (await workflow_waits.waits_of_step(s, step.id))[-1]
    env = Env(world.sm, world.settings, Who(tenant_id=world.tenant_id, account_id=world.owner), NOW)
    detail = await inbox.item(env, wait.id)
    assert detail.form == {
        "type": "object",
        "required": ["channel"],
        "properties": {
            "channel": {"type": "string", "title": "Channel", "enum": ["#delivery", "#leads"]},
            "note": {"type": "string", "title": "Note"},
        },
    }, f"the web form asks the fields in order: {detail.form}"
    assert detail.form and list(detail.form["properties"]) == ["channel", "note"], "author order"
    assert detail.values == {"channel": "#delivery"}, "a required choice starts on its first"
    async with world.sm() as s:
        [post] = await workflow_deliveries.deliveries_of_wait(s, wait.id)
    form = answers.form_of(post.payload["ask"], waits.ANSWER)
    assert form is not None and [(b.name, b.title, b.choices, b.required) for b in form.fields] == [
        ("channel", "Channel", ("#delivery", "#leads"), True),
        ("note", "Note", (), False),
    ], f"the chat modal asks the same fields: {form}"

    who = Who(tenant_id=world.tenant_id, account_id=world.owner)
    wrong = waits.Answer(route="answer", values={"channel": "#nope"})
    with pytest.raises(waits.BadAnswer, match="Pick one of the choices for Channel"):
        await waits.decide_wait(world.sm, world.settings, who, wait.id, wrong, via="web", now=NOW)
    answer = waits.Answer(route="answer", values={"note": "Kickoff Monday", "channel": "#leads"})
    await waits.decide_wait(world.sm, world.settings, who, wait.id, answer, via="web", now=NOW)
    await drive(world, run.id, lane)
    assert told and "<answer>Channel: #leads\nNote: Kickoff Monday</answer>" in told[0], told
    done = (await inbox.item(env, wait.id)).answer
    assert done is not None and list(done["values"].items()) == [
        ("channel", "#leads"),
        ("note", "Kickoff Monday"),
    ], f"the answer keeps the fields' order: {done}"
    assert done["labels"] == {"channel": "Channel", "note": "Note"}, "shown under their titles"
