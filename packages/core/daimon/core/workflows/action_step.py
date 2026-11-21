"""Third-party calls a workflow makes itself: Action steps, and the writes an agent step
proposed once a person approved them. Every such call goes through
`decide_unattended_call` first (a contract test holds this module to it)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from daimon.core.config import Settings
from daimon.core.stores import workflow_proposals, workflow_runs, workflow_waits
from daimon.core.stores.workflow_run_events import NewEvent, append_events, list_events
from daimon.core.workflows import asks, engine, gate
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.runs import ENGINE, Park, Sessions, StepWork, key_of
from daimon.core.workflows.spec import ActionNode, AgentNode, WriteScope, parse_duration
from daimon.core.workflows.unattended import (
    CREATED,
    Call,
    bound_by,
    call_hash,
    created_by_tool,
    created_ids,
    created_paths,
    decide_unattended_call,
    exposed_at_run,
)
from daimon.core.workflows.waits import (
    PROPOSALS,
    REVIEW_EXPIRES,
    REVIEW_REMIND,
    approvers_of,
    bound_text,
    home_of,
    route_labels,
    routes_of,
)
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_TIMEOUT_S: Final = 60
MAX_TIMEOUT_S: Final = 900


class ToolFailed(Exception):
    """The call failed; the message says why, never the server's own error text."""


class ToolClient(Protocol):
    async def server_url(self, work: StepWork, server: str, /) -> str | None:
        """The URL of the step agent's attached server `server`, or None."""
        ...

    async def call(
        self, work: StepWork, url: str, tool: str, args: dict[str, JsonValue], /
    ) -> JsonValue:
        """Call `tool` on the server at `url` with the step agent's credential."""
        ...


def exposed(work: StepWork) -> bool:
    return exposed_at_run(
        work.spec,
        work.step.node_id,
        untrusted_inputs=work.run.untrusted_inputs,
        catalog=work.catalog,
    )


async def step_scopes(
    sm: Sessions, settings: Settings, work: StepWork, node: ActionNode | AgentNode
) -> list[WriteScope]:
    """The step's `writes_to`, granted at publish; none while scoped writes are off."""
    if not node.writes_to:
        return []
    features = await gate.features_at(sm, settings, work.run.tenant_id)
    return list(node.writes_to) if features is not None and features.scoped_writes else []


async def created_so_far(sm: Sessions, run_id: uuid.UUID) -> dict[str, set[str]]:
    """The ids this run's allowed calls returned, from its ledger."""
    async with sm() as s:
        events = await list_events(s, run_id, types=[CREATED])
    return created_by_tool(e.data for e in events)


async def record_created(
    sm: Sessions, work: StepWork, tool: str, ids: list[str], now: datetime
) -> None:
    """Keep the ids a call made in the ledger, for a later `created_by` rule."""
    if ids:
        event = NewEvent(CREATED, key_of(work.step), {"tool": tool, "ids": ids})
        async with sm.begin() as s:
            await append_events(s, run_id=work.run.id, events=[event], actor=ENGINE, at=now)


async def _approved_calls(
    session: AsyncSession, run_id: uuid.UUID, approval: str
) -> dict[str, Any]:
    """The calls the latest approval of `approval` covers, when its digest still holds."""
    steps = [
        s
        for s in await workflow_runs.list_steps(session, run_id)
        if s.node_id == approval and s.status == "succeeded"
    ]
    if not steps:
        return {}
    latest = max(steps, key=lambda s: s.visit)
    decided = [w for w in await workflow_waits.waits_of_step(session, latest.id) if w.shown_hash]
    if not decided:
        return {}
    calls: dict[str, Any] = (decided[-1].shown or {}).get("binds") or {}
    return calls if call_hash("binds", calls) == decided[-1].shown_hash else {}


@dataclass(frozen=True)
class ActionStepExecutor:
    """One tool call with rendered arguments: a read, or a write under the Action's grant."""

    sm: Sessions
    settings: Settings
    tools: ToolClient
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, work: StepWork) -> engine.Outcome:
        node = work.node
        assert isinstance(node, ActionNode), "the action executor runs action nodes"
        server, tool = node.tool.split("/", 1)
        try:
            args = render(dict(node.args or {}), work.context, now=self.clock())
        except ExpressionError as err:
            return engine.Failed("fatal", str(err))
        assert isinstance(args, dict)
        approved = False
        if (approval := bound_by(work.spec, work.step.node_id)) is not None:
            async with self.sm() as s:
                calls = await _approved_calls(s, work.run.id, approval)
            entry: dict[str, Any] = calls.get(work.step.node_id) or {}
            if entry.get("hash") != call_hash(node.tool, args):
                return engine.Failed(
                    "fatal",
                    "The call differs from what was approved. Run it again to ask again.",
                    reason="approval_mismatch",
                )
            approved = True
        scopes = await step_scopes(self.sm, self.settings, work, node)
        decision = decide_unattended_call(
            self.settings.tool_safety,
            Call(
                server,
                tool,
                # A scoped-only Action has no write grant beyond its scopes.
                "refuse" if any(s.tool == node.tool for s in node.writes_to or []) else "allow",
                test=work.run.mode == "test",
                exposed=exposed(work),
                approved=approved,
                args=args,
                scopes=scopes,
                created=await created_so_far(self.sm, work.run.id) if scopes else {},
            ),
        )
        if decision.reason == "test_run":
            return engine.Succeeded(
                {"sent": False, "would_send": {"tool": node.tool, "args": args}}
            )
        if decision.verdict == "propose":
            return engine.Failed(
                "fatal",
                "Untrusted data can steer this Action. Bind it to an approval.",
                reason="grant_needs_review",
            )
        if decision.verdict == "refuse":
            return engine.Failed("fatal", decision.message, reason=decision.reason)
        url = await self.tools.server_url(work, server)
        if url is None:
            return engine.Failed("fatal", f"The agent does not connect {server}.")
        if not node.idempotent:
            async with self.sm.begin() as s:
                await workflow_runs.mark_send_intent(s, step_id=work.step.id, now=self.clock())
        timeout = parse_duration(node.timeout).total_seconds() if node.timeout else 0
        try:
            async with asyncio.timeout(min(timeout or DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)):
                result = await self.tools.call(work, url, tool, args)
        except (ToolFailed, TimeoutError) as err:
            message = str(err) or "The call timed out"
            if node.idempotent:
                return engine.Failed("retryable", message)
            return engine.Unknown(f"{message}. It may have gone out; check before running again.")
        ids = created_ids(created_paths(work.spec).get(node.tool, ()), result)
        await record_created(self.sm, work, node.tool, ids, self.clock())
        return engine.Succeeded({"sent": True, "result": result})


# ---------- an agent step's proposals ----------


async def review_proposals(
    sm: Sessions,
    settings: Settings,
    tools: ToolClient | None,
    work: StepWork,
    outcome: engine.Succeeded,
    now: datetime,
) -> engine.Outcome | Park | None:
    """After the turn: open the step's review of its proposed writes, or send them at once
    for `review: auto` on a step untrusted data cannot steer. None when nothing was proposed."""
    node = work.node
    assert isinstance(node, AgentNode)
    async with sm() as s:
        pending = [
            p
            for p in await workflow_proposals.proposals_of(s, work.step.id)
            if p.status == "proposed"
        ]
    if not pending:
        return None
    if node.review == "auto" and not exposed(work):
        async with sm.begin() as s:
            await workflow_proposals.decide_proposals(
                s, work.step.id, approved=[p.id for p in pending], by=None, now=now
            )
        return await send_proposals(sm, settings, tools, work, _held(outcome), now)
    approvers = node.review if isinstance(node.review, list) else []
    async with sm() as s:
        addressees, tokens, workflow = await approvers_of(s, work, approvers, now)
    title = node.title or work.step.node_id
    calls: list[dict[str, JsonValue]] = [
        {"tool": p.tool, "args": p.arguments, "reason": p.reason} for p in pending
    ]
    ask = asks.Ask(
        deliver="dm",
        workflow=workflow.name,
        title=title,
        text="\n\n".join(
            (title, bound_text(calls), "To send only some, pick them in the web inbox.")
        ),
        shape="approve",
        routes=route_labels(routes_of(node, PROPOSALS)),
        home=home_of(work.spec, workflow),
    )
    return Park(
        "approval",
        addressees=tuple(addressees),
        deliver=ask.deliver,
        ask=ask,
        shown={"kind": PROPOSALS, "approvers": tokens, "proposals": [str(p.id) for p in pending]}
        | _held(outcome),
        expires_at=now + REVIEW_EXPIRES,
        remind_at=now + REVIEW_REMIND,
    )


def _held(outcome: engine.Succeeded) -> dict[str, Any]:
    return {"output": outcome.output, "route": outcome.route, "summary": outcome.summary}


async def reviewed(sm: Sessions, work: StepWork) -> dict[str, Any] | None:
    """The step's decided proposals review, whose turn already ran."""
    async with sm() as s:
        waits = await workflow_waits.waits_of_step(s, work.step.id)
    done = [
        w for w in waits if (w.shown or {}).get("kind") == PROPOSALS and w.status == "completed"
    ]
    return done[-1].shown if done else None


async def send_proposals(
    sm: Sessions,
    settings: Settings,
    tools: ToolClient | None,
    work: StepWork,
    held: dict[str, Any],
    now: datetime,
) -> engine.Outcome:
    """Send the step's approved proposals, each once; the step then settles as its turn did."""
    async with sm() as s:
        approved = [
            p
            for p in await workflow_proposals.proposals_of(s, work.step.id)
            if p.status == "approved"
        ]
    sent = failed = 0
    for proposal in approved:
        server, tool = proposal.tool.split("/", 1)
        decision = decide_unattended_call(
            settings.tool_safety,
            Call(server, tool, "propose", test=work.run.mode == "test", approved=True),
        )
        status: workflow_proposals.Status = "failed"
        result: dict[str, Any] | None = {"error": decision.message}
        if decision.verdict == "allow" and tools is not None:
            async with sm.begin() as s:
                await workflow_runs.mark_send_intent(s, step_id=work.step.id, now=now)
            try:
                value = await tools.call(work, proposal.server_url, tool, proposal.arguments)
                status, result = "sent", {"result": value}
            except (ToolFailed, TimeoutError) as err:
                result = {"error": str(err) or "The call failed"}
        async with sm.begin() as s:
            await workflow_proposals.settle_proposal(
                s, proposal.id, status=status, result=result, now=now
            )
        sent, failed = (sent + 1, failed) if status == "sent" else (sent, failed + 1)
    note = f"{sent} proposed writes sent" + (f", {failed} failed" if failed else "")
    summary = held.get("summary")
    return engine.Succeeded(
        held.get("output"),
        route=held.get("route"),
        summary=f"{summary}\n{note}" if summary else note,
    )
