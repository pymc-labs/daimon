"""Design use cases 5 to 8 and 10, end to end on the database with fake agents and tools.

Code runs for real through its fixed wrapper; agents, actions and approvals answer from a
script per node. Use case 10 stops at its event wait, which the waits milestone runs. Built-in tools
run under grants recorded for the live version, as an admin's approval leaves them.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

from daimon.core.stores.workflow_runs import RunRow
from daimon.core.workflows import engine, runs
from daimon.core.workflows.code_step import CodeJob, CodeStepExecutor
from daimon.testing.workflows import NOW, World, make_world
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

Answers = dict[str, list[engine.Outcome]]


async def drive(
    world: World,
    run_id: uuid.UUID,
    answers: Answers,
    *,
    outputs: Path | None = None,
    stop_at: frozenset[str] = frozenset(),
) -> RunRow:
    """Run every ready step until none is left; each node answers from its script."""

    async def fake(work: runs.StepWork) -> engine.Outcome:
        return answers[work.step.node_id].pop(0)

    async def local(_work: runs.StepWork, job: CodeJob) -> bytes:
        assert outputs is not None
        command = job.command.replace("/mnt/session/outputs", str(outputs))
        subprocess.run(command, shell=True, check=True, capture_output=True)
        return (outputs / f"workflow-code-{job.nonce}.json").read_bytes()

    executors: dict[str, runs.StepExecutor] = dict.fromkeys(("agent", "action", "approval"), fake)
    executors["code"] = CodeStepExecutor(world.sm, local, clock=lambda: NOW)
    for _ in range(40):
        rows = (await world.steps(run_id)) | (await world.items(run_id))
        ready = [k for k, r in rows.items() if r.status == "ready" and r.node_id not in stop_at]
        if not ready:
            break
        for key in ready:
            claimed = await world.claim(run_id, key)
            await runs.execute(world.sm, world.settings, executors, claimed, clock=lambda: NOW)
    return await world.get(run_id)


MARGINS = """import json
rows = json.load(open("input.json"))["projects"]
table = [dict(name=r["name"], margin=round(1 - r["cost"] / (r["hours"] * r["rate"]), 2)) for r in rows]
json.dump(dict(table=table, flags=[t["name"] for t in table if t["margin"] < 0.3]), open("output.json", "w"))
"""


async def test_5_monthly_profitability_takes_numbers_from_code(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    nodes: dict[str, Any] = {
        "fetch": {"kind": "action", "tool": "timetrack/hours", "next": "compute"},
        "compute": {
            "kind": "code",
            "source": MARGINS,
            "inputs": {"projects": "${{ steps.fetch.output.projects }}"},
            "output": {"table": {"type": "array"}, "flags": {"type": "array"}},
            "next": "explain",
        },
        "explain": {
            "kind": "agent",
            "instructions": "Explain the table",
            "inputs": {"table": "${{ steps.compute.output.table }}"},
            "next": "done",
        },
        "done": {
            "kind": "finish",
            "output": "${{ {'flags': steps.compute.output.flags, 'text': steps.explain.summary} }}",
        },
    }
    world = await make_world(db_session, nodes, "fetch")
    world.settings = world.settings.model_copy(
        update={"features": {"workflows": "on", "workflow_code": "on"}}
    )
    projects: list[JsonValue] = [
        {"name": "A", "hours": 10, "rate": 100, "cost": 500},
        {"name": "B", "hours": 10, "rate": 100, "cost": 900},
    ]
    answers: Answers = {
        "fetch": [engine.Succeeded({"projects": projects})],
        "explain": [engine.Succeeded(summary="B runs thin")],
    }
    done = await drive(world, (await world.run()).id, answers, outputs=tmp_path)
    assert done.status == "succeeded", f"{done}"
    output = (await world.steps(done.id))["done"].output
    assert output == {"flags": ["B"], "text": "B runs thin"}, "the figures come from the script"


async def test_6_issue_triage_routes_untrusted_input_and_ends_quiet_on_spam(
    db_session: AsyncSession,
) -> None:
    nodes: dict[str, Any] = {
        "classify": {
            "kind": "agent",
            "instructions": "Classify ${{ inputs.title }}",
            "routes": {"bug": "reproduce", "spam": "quiet"},
        },
        "reproduce": {
            "kind": "agent",
            "instructions": "Reproduce",
            "builtins": "all",
            "session": "fresh",
            "next": "respond",
        },
        "respond": {"kind": "agent", "instructions": "Label", "writes": "propose", "next": "done"},
        "done": {"kind": "finish", "output": "${{ steps.reproduce.output }}"},
        "quiet": {"kind": "finish"},
    }
    world = await make_world(db_session, nodes, "classify")
    assert await world.grant_all() == ["builtins"], "the sandbox step runs under its grant"
    bug = await world.run(inputs={"title": "Crash"}, untrusted_inputs=True)
    answers: Answers = {
        "classify": [engine.Succeeded(route="bug"), engine.Succeeded(route="spam")],
        "reproduce": [engine.Succeeded({"result": "reproduced"})],
        "respond": [engine.Succeeded(summary="Labelled")],
    }
    done = await drive(world, bug.id, answers)
    assert (done.status, done.untrusted_inputs) == ("succeeded", True), f"{done}"
    assert (await world.steps(done.id))["done"].output == {"result": "reproduced"}
    spam = await drive(world, (await world.run(inputs={"title": "Buy"})).id, answers)
    steps = await world.steps(spam.id)
    assert spam.status == "succeeded" and steps["reproduce"].status == "skipped", "spam is quiet"


async def test_7_competitor_research_fans_out_and_tolerates_one_failure(
    db_session: AsyncSession,
) -> None:
    nodes: dict[str, Any] = {
        "each": {
            "kind": "for_each",
            "items": "${{ inputs.competitors }}",
            "do": {"kind": "agent", "instructions": "Research", "builtins": "read"},
            "concurrency": 5,
            "tolerate_failures": 1,
            "next": "synthesise",
        },
        "synthesise": {
            "kind": "agent",
            "instructions": "Compare",
            "session": "fresh",
            "inputs": {"found": "${{ steps.each.output }}"},
            "next": "done",
        },
        "done": {"kind": "finish", "output": "${{ steps.each.output }}"},
    }
    world = await make_world(db_session, nodes, "each")
    await world.grant_all()
    run = await world.run(inputs={"competitors": ["Acme", "Globex", "Initech"]})
    answers: Answers = {
        "each": [
            engine.Succeeded({"pricing": "low"}),
            engine.Failed("agent", "No site"),
            engine.Succeeded({"pricing": "high"}),
        ],
        "synthesise": [engine.Succeeded(summary="Acme is cheapest")],
    }
    done = await drive(world, run.id, answers)
    assert done.status == "succeeded", f"{done}"
    output = (await world.steps(run.id))["done"].output
    assert output == [{"pricing": "low"}, None, {"pricing": "high"}], "item order, null gap"
    await world.verified(run.id)


async def test_8_content_review_loops_until_the_critic_accepts(db_session: AsyncSession) -> None:
    nodes: dict[str, Any] = {
        "write": {"kind": "agent", "instructions": "Draft", "max_visits": 3, "next": "critique"},
        "critique": {
            "kind": "agent",
            "agent": "critic",
            "instructions": "Critique",
            "routes": {"revise": "write", "accept": "review"},
            "max_visits": 3,
            "on_max_visits": "review",
        },
        "review": {
            "kind": "approval",
            "approvers": ["owner"],
            "binds": "publish",
            "routes": {"approve": "publish", "reject": "rejected"},
        },
        "publish": {"kind": "action", "tool": "docs/create_page", "next": "done"},
        "done": {"kind": "finish", "output": "${{ steps.publish.output }}"},
        "rejected": {"kind": "finish", "outcome": "failed"},
    }
    world = await make_world(db_session, nodes, "write")
    answers: Answers = {
        "write": [engine.Succeeded(summary="v1"), engine.Succeeded(summary="v2")],
        "critique": [engine.Succeeded(route="revise"), engine.Succeeded(route="accept")],
        "review": [engine.Succeeded(route="approve")],
        "publish": [engine.Succeeded({"url": "https://example.com/page"})],
    }
    done = await drive(world, (await world.run()).id, answers)
    assert done.status == "succeeded", f"{done}"
    steps = await world.steps(done.id)
    assert {"write#2", "critique#2"} <= steps.keys(), "the loop ran twice"
    assert steps["done"].output == {"url": "https://example.com/page"}


async def test_10_alert_triage_pages_high_and_waits_on_low(db_session: AsyncSession) -> None:
    nodes: dict[str, Any] = {
        "investigate": {
            "kind": "agent",
            "instructions": "Investigate",
            "builtins": "read",
            "output": {"severity": {"type": "string"}},
            "next": "severity",
        },
        "severity": {
            "kind": "branch",
            "cases": [{"when": "${{ steps.investigate.output.severity == 'high' }}", "to": "page"}],
            "else": "watch",
        },
        "page": {"kind": "finish"},
        "watch": {
            "kind": "wait",
            "event": {"source": "webhook:alerts", "match": "${{ event.status == 'resolved' }}"},
            "timeout": "2h",
            "routes": {"arrived": "quiet", "timed_out": "page"},
        },
        "quiet": {"kind": "finish", "outcome": "succeeded"},
    }
    world = await make_world(db_session, nodes, "investigate")
    await world.grant_all()
    answers: Answers = {
        "investigate": [
            engine.Succeeded({"severity": "high"}),
            engine.Succeeded({"severity": "low"}),
        ]
    }
    high = await drive(world, (await world.run()).id, answers)
    assert high.status == "succeeded" and "page" in await world.steps(high.id), f"{high}"
    low = await drive(world, (await world.run()).id, answers, stop_at=frozenset({"watch"}))
    assert (await world.steps(low.id))["watch"].status == "ready", "it reaches the wait"
