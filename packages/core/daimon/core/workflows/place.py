"""An agent step's place as a fire reads it: the run-as person at the step's `at`.

`fire.admit.admit_place` decides on it, as on a routine's row, so a step and a routine at
the same place get the same verdict.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from daimon.core.fire.admit import Admitted
from daimon.core.fire.subject import Subject
from daimon.core.routine_sandbox import RoutineSandbox
from daimon.core.stores import identity, tenants, workflow_runs, workflows
from daimon.core.stores.domain import RoutineDestinationKind
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.workflows.delivery import target_for
from daimon.core.workflows.runs import node_of, run_spec
from daimon.core.workflows.spec import AgentNode
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class StepPlace:
    """Where admission let one step attempt work, as its brief and its turn read it."""

    #: The channel or thread id, None for a step that works nowhere (private).
    where: str | None = None
    #: Whether the agent is invited to post there itself.
    may_post: bool = False
    subject: Subject | None = None
    admitted: Admitted | None = None
    agent_id: str = ""
    environment_id: str = ""
    #: The built-ins grant's pin a live run must match to keep them.
    sandbox: RoutineSandbox | None = None
    #: A private step's run-as person's Discord DM, for a run that posts to them: what
    #: its relay credential signs room `dm` for.
    dm_channel: str | None = None


def step_place(at: str | None, *, home: str | None, origin: Mapping[str, Any] | None) -> str | None:
    """The channel or thread id `at` names: `origin` is the run's start thread, else home."""
    if at == "home":
        return home
    if at == "origin":
        found = origin and (origin.get("thread") or origin.get("channel"))
        return str(found) if found else home
    return at


def workflow_subject(
    workflow: WorkflowRow,
    node: AgentNode,
    *,
    home: str | None,
    run_as: str | None,
    agent_name: str,
    agent_id: str,
    platform: str,
    origin: Mapping[str, Any] | None = None,
) -> Subject:
    """The step's facts: `run_as` (their platform id) runs `agent_name` at `at`.

    A Slack or Teams thread names its channel. On Discord, home is a channel and the run's
    start thread sits under its channel. The destination of the routine the workflow was
    moved from is placed as that routine saved it: a channel as itself, a thread under its
    saved channel, which the routine fire admits it at. Any other Discord id is taken as a
    thread under home (the stricter reading), else a channel. No `at`: no place, so no
    origin stamp and no hold.
    """
    place = step_place(node.at, home=home, origin=origin)
    kind: RoutineDestinationKind | None = None
    channel: str | None = None
    if place is not None:
        target = target_for(place, home=home, mode="live", run_as=None, owner=None)
        kind, channel = target["kind"], target["channel_id"]
        if kind == "channel" and platform == "discord" and place != home:
            kind, channel = _discord_place(workflow, place, home=home, origin=origin)
    made = workflow.made_in
    return Subject(
        id=workflow.id,
        tenant_id=workflow.tenant_id,
        created_by_user_id=run_as,
        agent_id=agent_id,
        agent_name=agent_name,
        cron_expr="",
        timezone="UTC",
        trigger_message=node.instructions,
        name=workflow.name,
        destination_kind=kind,
        destination_id=place,
        channel_id=channel,
        source_provenance_known=made is not None and made["known"],
        source_channel_id=made["channel_id"] if made is not None else None,
        source_thread_id=made["thread_id"] if made is not None else None,
        revision=workflow.updated_at.isoformat(),
    )


def _discord_place(
    workflow: WorkflowRow, place: str, *, home: str | None, origin: Mapping[str, Any] | None
) -> tuple[RoutineDestinationKind, str | None]:
    """A Discord id's kind and the channel admission places it in (`workflow_subject`)."""
    moved = workflow.converted_from or {}
    to = moved.get("to") or {}
    if to.get("id") == place and to.get("kind") in ("channel", "thread"):
        return to["kind"], moved.get("channel_id")
    if origin and origin.get("thread") == place and origin.get("channel"):
        return "thread", str(origin["channel"])
    return ("thread", home) if home is not None else ("channel", place)


@dataclass(frozen=True)
class BoundStep:
    """The agent step a workflow run's credential serves, as its own checks read it."""

    workflow: WorkflowRow
    subject: Subject
    #: The step's `at`; None for a private step.
    at: str | None


async def bound_step(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    agent_name: str = "",
    agent_id: str = "",
) -> BoundStep | None:
    """The one agent step `run_id` runs now, read as a routine-bound relay credential reads its
    routine row: the run-as person read live, at the step's `at`.

    None for a run of another tenant or no longer running, a live run whose workflow
    publishes another version now, a run-as person with no id here, or not exactly one
    agent step running: steps in parallel sign nothing rather than guess.
    """
    run = await workflow_runs.get_run(session, run_id, tenant_id=tenant_id)
    if run is None or run.status != "running" or run.run_as_account_id is None:
        return None
    flow = await workflows.get_workflow(session, tenant_id=tenant_id, workflow_id=run.workflow_id)
    tenant = await tenants.get_tenant(session, tenant_id)
    if flow is None or tenant is None or (run.mode == "live" and flow.live_version != run.version):
        return None
    spec = await run_spec(session, run)
    nodes = [
        node
        for step in await workflow_runs.list_steps(session, run.id)
        if step.status == "running" and isinstance(node := node_of(spec, step), AgentNode)
    ]
    ids = await identity.platform_external_ids(
        session, tenant_id=tenant_id, platform=tenant.platform, account_ids=[run.run_as_account_id]
    )
    run_as = ids.get(run.run_as_account_id)
    if len(nodes) != 1 or run_as is None:
        return None
    subject = workflow_subject(
        flow,
        nodes[0],
        home=flow.home_channel_id,
        run_as=run_as,
        agent_name=agent_name,
        agent_id=agent_id,
        platform=tenant.platform,
        origin=run.origin,
    )
    return BoundStep(flow, subject, nodes[0].at)
