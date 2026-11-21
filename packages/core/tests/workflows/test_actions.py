"""Actions, bound approvals and proposals on the database: design use cases 3 and 9, and
the one rule every unattended third-party call goes through."""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from daimon.core._models import Account
from daimon.core.stores import (
    workflow_deliveries,
    workflow_proposals,
    workflow_runs,
    workflow_waits,
    workflows,
)
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from daimon.core.workflows import engine, inbox, runs, service, views, waits
from daimon.core.workflows.access import Who
from daimon.core.workflows.action_step import (
    ActionStepExecutor,
    review_proposals,
    reviewed,
    send_proposals,
)
from daimon.core.workflows.agent_step import step_decider
from daimon.core.workflows.service import Env
from daimon.core.workflows.spec import AgentNode
from daimon.core.workflows.unattended import Call, decide_unattended_call
from daimon.testing.workflows import NOW, World, make_world
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

Answers = dict[str, list[engine.Outcome]]


@dataclass
class FakeTools:
    calls: list[tuple[str, str, dict[str, JsonValue]]] = field(
        default_factory=list[tuple[str, str, dict[str, JsonValue]]]
    )

    async def server_url(self, work: runs.StepWork, server: str) -> str | None:
        return f"https://{server}.example.com/mcp"

    async def call(
        self, work: runs.StepWork, url: str, tool: str, args: dict[str, JsonValue]
    ) -> JsonValue:
        self.calls.append((url, tool, args))
        return {"ok": True}


async def drive(
    world: World, run_id: uuid.UUID, answers: Answers, tools: FakeTools, *, tamper: bool = False
) -> list[uuid.UUID]:
    """Run ready steps with real approval, wait and action executors; the owner approves each
    approval as it opens. Returns the waits approved."""

    async def fake(work: runs.StepWork) -> engine.Outcome:
        return answers[work.step.node_id].pop(0)

    executors: dict[str, runs.StepExecutor] = {
        "agent": fake,
        "action": ActionStepExecutor(world.sm, world.settings, tools, clock=lambda: NOW),
        **waits.executors(world.sm),
    }
    approved: list[uuid.UUID] = []
    asked: set[uuid.UUID] = set()
    for _ in range(40):
        rows = await world.steps(run_id)
        ready = [k for k, r in rows.items() if r.status == "ready"]
        asking = [r for r in rows.values() if r.status == "waiting" and r.id not in asked]
        for step in asking:
            asked.add(step.id)
            async with world.sm() as s:
                wait = (await workflow_waits.waits_of_step(s, step.id))[-1]
            answer = waits.Answer(route=waits.APPROVE)
            who = Who(world.tenant_id, world.owner)
            await waits.decide_wait(
                world.sm, world.settings, who, wait.id, answer, via="web", now=NOW
            )
            if tamper:
                await _tamper(world, wait.id)
            approved.append(wait.id)
        if not ready and not asking:
            break
        for key in ready:
            claimed = await world.claim(run_id, key)
            await runs.execute(world.sm, world.settings, executors, claimed, clock=lambda: NOW)
    return approved


async def _tamper(world: World, wait_id: uuid.UUID) -> None:
    """Change an approved call, keeping the wait's digest consistent with it."""
    from daimon.core._models import WorkflowWait
    from daimon.core.workflows.unattended import call_hash

    async with world.sm.begin() as s:
        row = await s.get(WorkflowWait, wait_id)
        assert row is not None and row.shown is not None
        binds = {k: v | {"hash": "changed"} for k, v in row.shown["binds"].items()}
        row.shown = row.shown | {"binds": binds}
        row.shown_hash = call_hash("binds", binds)


LEAD: dict[str, Any] = {
    "research": {
        "kind": "agent",
        "instructions": "Research ${{ inputs.email }} and draft a reply",
        "output": {"subject": {"type": "string"}, "body": {"type": "string"}},
        "next": "review",
    },
    "review": {
        "kind": "approval",
        "approvers": ["owner"],
        "binds": "send",
        "show": "${{ steps.research.output.body }}",
        "routes": {"approve": "send", "reject": "stopped"},
    },
    "send": {
        "kind": "action",
        "tool": "gmail/send_message",
        "args": {
            "to": "${{ inputs.email }}",
            "subject": "${{ steps.research.output.subject }}",
            "body": "${{ steps.research.output.body }}",
        },
        "next": "done",
    },
    "done": {"kind": "finish", "output": "${{ steps.send.output }}"},
    "stopped": {"kind": "finish", "outcome": "failed"},
}
DRAFT = engine.Succeeded({"subject": "Hello", "body": "Thanks for writing"})


async def test_3_inbound_lead_sends_exactly_the_approved_email(db_session: AsyncSession) -> None:
    world = await make_world(db_session, LEAD, "research")
    tools = FakeTools()
    run = await world.run(inputs={"email": "lead@example.com"}, untrusted_inputs=True)
    approved = await drive(world, run.id, {"research": [DRAFT]}, tools)
    done = await world.get(run.id)
    assert done.status == "succeeded" and len(approved) == 1, f"{done}"
    sent = {"to": "lead@example.com", "subject": "Hello", "body": "Thanks for writing"}
    assert tools.calls == [("https://gmail.example.com/mcp", "send_message", sent)]
    async with world.sm() as s:
        wait = await workflow_waits.get_wait(s, approved[0], tenant_id=world.tenant_id)
    assert wait is not None and wait.shown_hash, "the approval records what it covers"
    async with world.sm() as s:
        [ask] = await workflow_deliveries.deliveries_of_wait(s, approved[0])
    text = str(ask.payload["text"])
    assert "Exactly what runs on approval" in text and "lead@example.com" in text, (
        f"the chat ask shows the call as the inbox does: {text}"
    )

    tampered = await world.run(inputs={"email": "lead@example.com"}, untrusted_inputs=True)
    await drive(world, tampered.id, {"research": [DRAFT]}, FakeTools(), tamper=True)
    failed = await world.get(tampered.id)
    send = (await world.steps(tampered.id))["send"]
    assert (failed.status, send.status) == ("failed", "failed"), "a changed call is not sent"
    assert "differs from what was approved" in (send.error_message or "")


ONBOARDING: dict[str, Any] = {
    "confirm": {
        "kind": "approval",
        "approvers": ["owner"],
        "binds": ["folder", "page", "project"],
        "routes": {"approve": "fan", "reject": "stopped"},
    },
    "fan": {
        "kind": "transform",
        "set": {"client": "${{ inputs.client }}"},
        "next": ["folder", "page", "project"],
    },
    "folder": {
        "kind": "action",
        "tool": "drive/create_folder",
        "args": {"name": "${{ inputs.client }}"},
        "next": "welcome",
    },
    "page": {
        "kind": "action",
        "tool": "docs/create_page",
        "args": {"title": "${{ inputs.client }}"},
        "next": "welcome",
    },
    "project": {
        "kind": "action",
        "tool": "timetrack/create_project",
        "args": {"name": "${{ inputs.client }}"},
        "next": "welcome",
    },
    "welcome": {
        "kind": "agent",
        "instructions": "Draft a welcome",
        "output": {"body": {"type": "string"}},
        "next": "review",
    },
    "review": {
        "kind": "approval",
        "approvers": ["owner"],
        "binds": "send",
        "routes": {"approve": "send", "reject": "stopped"},
    },
    "send": {
        "kind": "action",
        "tool": "gmail/send_message",
        "args": {"body": "${{ steps.welcome.output.body }}"},
        "next": "before_kickoff",
    },
    "before_kickoff": {"kind": "wait", "until": "${{ inputs.kickoff }}", "next": "brief"},
    "brief": {"kind": "agent", "instructions": "Kickoff brief", "next": "done"},
    "done": {"kind": "finish", "output": "${{ steps.brief.summary }}"},
    "stopped": {"kind": "finish", "outcome": "failed"},
}


async def test_9_onboarding_by_another_person_needs_the_bound_confirm(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, ONBOARDING, "confirm")
    tools = FakeTools()
    run = await world.run(
        inputs={"client": "Acme", "kickoff": "2020-01-01T00:00:00Z"}, untrusted_inputs=True
    )
    answers: Answers = {
        "welcome": [engine.Succeeded({"body": "Welcome, Acme"})],
        "brief": [engine.Succeeded(summary="Ready for kickoff")],
    }
    approved = await drive(world, run.id, answers, tools)
    done = await world.get(run.id)
    assert done.status == "succeeded" and len(approved) == 2, f"{done}"
    assert sorted(t for _, t, _ in tools.calls) == [
        "create_folder",
        "create_page",
        "create_project",
        "send_message",
    ], "three bound actions in parallel, then the bound send"
    assert (await world.steps(run.id))["done"].output == "Ready for kickoff"


async def test_approved_proposals_are_sent_and_the_rest_are_not(db_session: AsyncSession) -> None:
    nodes: dict[str, Any] = {
        "file": {
            "kind": "agent",
            "instructions": "File issues",
            "writes": "propose",
            "next": "done",
        },
        "done": {"kind": "finish", "output": "${{ steps.file.output }}"},
    }
    world = await make_world(db_session, nodes, "file")
    tools = FakeTools()
    picked: list[uuid.UUID] = []

    async def agent(work: runs.StepWork) -> engine.Outcome | runs.Park:
        if (held := await reviewed(world.sm, work)) is not None:
            return await send_proposals(world.sm, world.settings, tools, work, held, NOW)
        async with world.sm.begin() as s:
            for title in ("A", "B", "A"):  # the same call twice is one proposal
                added = await workflow_proposals.add_proposal(
                    s,
                    tenant_id=work.run.tenant_id,
                    step_id=work.step.id,
                    server_url="https://linear.example.com/mcp",
                    tool="linear/create_issue",
                    arguments={"title": title},
                    reason="untrusted",
                )
                assert added is not None
                picked.append(added[1].id)
        done = engine.Succeeded({"filed": 2}, summary="Filed")
        review = await review_proposals(world.sm, world.settings, tools, work, done, NOW)
        assert isinstance(review, runs.Park), "a person reviews the writes"
        return review

    executors: dict[str, runs.StepExecutor] = {"agent": agent}
    run = await world.run()
    await runs.execute(world.sm, world.settings, executors, await world.claim(run.id, "file"))
    step = (await world.steps(run.id))["file"]
    async with world.sm() as s:
        wait = (await workflow_waits.waits_of_step(s, step.id))[-1]
    assert waits.kind_of(wait) == waits.PROPOSALS and len((wait.shown or {})["proposals"]) == 2
    async with world.sm() as s:
        [ask] = await workflow_deliveries.deliveries_of_wait(s, wait.id)
    text = str(ask.payload["text"])
    assert "linear create_issue" in text and '"title": "B"' in text, f"posted to chat: {text}"
    assert (wait.expires_at, wait.remind_at) == (
        NOW + waits.REVIEW_EXPIRES,
        NOW + waits.REVIEW_REMIND,
    ), "a review expires and reminds like an approval"
    due = NOW + waits.REVIEW_REMIND
    async with world.sm() as s:
        [row] = await workflow_runs.due_waits(s, kind="approval", now=due, limit=5)
    await waits.on_due(world.sm, world.settings, row, now=due)
    async with world.sm() as s:
        reminded = await workflow_waits.get_wait(s, wait.id, tenant_id=world.tenant_id)
    assert reminded is not None and reminded.remind_at == NOW + 2 * waits.REVIEW_REMIND
    answer = waits.Answer(route=waits.APPROVE, selected=[str(picked[0])])
    who = Who(world.tenant_id, world.owner)
    await waits.decide_wait(world.sm, world.settings, who, wait.id, answer, via="web", now=NOW)
    await runs.execute(world.sm, world.settings, executors, await world.claim(run.id, "file"))
    assert tools.calls == [("https://linear.example.com/mcp", "create_issue", {"title": "A"})]
    async with world.sm() as s:
        statuses = [(p.id, p.status) for p in await workflow_proposals.proposals_of(s, step.id)]
    assert statuses == [(picked[0], "sent"), (picked[1], "rejected")], "in order, the chosen sent"
    assert (await world.steps(run.id))["file"].output == {"filed": 2}, "the turn's output holds"


def test_one_rule_decides_every_unattended_call() -> None:
    policy = ToolSafetyPolicy(denied=("crm/delete_deal",))

    def verdict(**kw: Any) -> tuple[str, str | None]:
        d = decide_unattended_call(policy, Call("crm", kw.pop("tool", "create_deal"), **kw))
        return d.verdict, d.reason

    assert verdict(writes="allow", tool="search") == ("allow", None), "reads go out"
    assert verdict(writes="allow", tool="delete_deal", approved=True)[1] == "denied"
    assert verdict(writes="allow", test=True, approved=True)[1] == "test_run"
    assert verdict(writes="allow") == ("allow", None)
    assert verdict(writes="allow", exposed=True) == ("propose", "untrusted"), "Rule of Two"
    assert verdict(writes="allow", exposed=True, approved=True) == ("allow", None)
    assert verdict(writes="propose") == ("propose", None)
    assert verdict(writes="refuse")[1] == "writes_refused"


async def test_an_exposed_step_with_allow_proposes_its_writes() -> None:
    node = AgentNode.model_validate({"kind": "agent", "instructions": "x", "writes": "allow"})
    proposed: list[tuple[str, str | None]] = []

    async def propose(call: ToolCall, reason: str | None) -> ToolConfirmationResult:
        proposed.append((call.key, reason))
        return ToolConfirmationResult(allow=False, deny_message="Recorded")

    async def allow(call: ToolCall) -> ToolConfirmationResult:
        return ToolConfirmationResult(allow=True)

    decide = step_decider(
        node,
        attempt_id=uuid.uuid4(),
        policy=ToolSafetyPolicy(),
        writes="allow",
        propose=propose,
        exposed=True,
    )(PolicyApproval(decide=allow)).decide
    write = await decide(ToolCall(tool_use_id="t", server_name="crm", tool_name="create_deal"))
    read = await decide(ToolCall(tool_use_id="t", server_name="crm", tool_name="search"))
    assert (write.allow, read.allow, proposed) == (False, True, [("crm/create_deal", "untrusted")])


# Every module that calls a third-party tool for a workflow decides it first.
CALLERS = {"action_step.py"}


def test_every_action_call_and_proposal_send_passes_the_rule() -> None:
    root = Path(__file__).resolve().parents[2] / "daimon" / "core" / "workflows"
    found: set[str] = set()
    for path in root.glob("*.py"):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            names = {
                n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
            }
            tool_calls = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "call"
                and "tools" in ast.unparse(n.func.value)
            ]
            if tool_calls:
                found.add(path.name)
                assert "decide_unattended_call" in names, f"{path.name}:{fn.name} decides first"
    assert found == CALLERS, f"tool calls live where the rule is checked: {found}"
    client = (root / "tool_client.py").read_text()
    assert client.count("call_tool(") == 1, "the client is the one place that calls MCP"


async def test_a_write_grant_is_requested_approved_and_carried_over(
    db_session: AsyncSession,
) -> None:
    nodes: dict[str, Any] = {
        "send": {
            "kind": "action",
            "tool": "gmail/send_message",
            "args": {"to": "a@example.com"},
            "next": "done",
        },
        "done": {"kind": "finish"},
    }
    world = await make_world(db_session, nodes, "send")
    admin = Account(tenant_id=world.tenant_id)
    db_session.add(admin)
    await db_session.commit()
    owner = Env(world.sm, world.settings, Who(world.tenant_id, world.owner), NOW)
    approver = Env(world.sm, world.settings, Who(world.tenant_id, admin.id, admin=True), NOW)

    async def status() -> str:
        async with world.sm() as s:
            (summary,) = await views.list_summaries(s, owner.who, now=NOW, default_budget_usd=5.0)
        return summary.status

    assert await status() == "needs_review", "the live version lacks its write grant"
    draft = await service.get_draft(owner, world.workflow_id)
    assert [g.kind for g in draft.new_grants] == ["unattended_write"], "the draft says so"
    with pytest.raises(service.Refused, match="Request approval"):
        await service.publish(owner, world.workflow_id, "Ship")
    await service.request_publish(owner, world.workflow_id, "Ship")
    (item,) = await inbox.list_items(approver)
    await inbox.decide(approver, uuid.UUID(item.id), inbox.Decide(route="approve"))
    assert await status() == "live", "the admin's approval granted the write"
    tools = FakeTools()
    run = await world.run()
    await drive(world, run.id, {}, tools)
    assert [t for _, t, _ in tools.calls] == ["send_message"], "it runs under the grant"
    assert (await world.get(run.id)).outcome == "completed", "a run that acted did something"
    draft = await service.get_draft(owner, world.workflow_id)
    assert draft.new_grants == [], "an unchanged node carries its grant over"
    await service.publish(owner, world.workflow_id, "Again")
    assert await status() == "live", "an unchanged node keeps its grant"


async def test_a_budget_over_the_default_marks_the_workflow_unreviewed(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, {"done": {"kind": "finish"}}, "done")
    async with world.sm() as s:
        flow = await workflows.get_workflow(
            s, tenant_id=world.tenant_id, workflow_id=world.workflow_id
        )
        assert flow is not None and flow.live_version is not None
        live = await workflows.get_version(
            s, tenant_id=world.tenant_id, workflow_id=flow.id, number=flow.live_version
        )
        assert live is not None
        over = live.model_copy(update={"spec": live.spec | {"limits": {"budget_usd": 50}}})
        assert await views.unreviewed(s, flow, over, 5.0), "its budget grant is missing"
        assert not await views.unreviewed(s, flow, over, 100.0), "within the default"
