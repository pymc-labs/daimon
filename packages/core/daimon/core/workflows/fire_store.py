"""`FireStore` over a workflow run's routine step: the fire core's writes, on workflow state.

Results and the channel's failure notice are `result` deliveries, the owner's notice a
`notice` one, the failure streak the run ledger (`converted_from.last` before the first run),
the lapsed-role pause the workflow's status and the grant keyed by the run. Every write is
fenced on the run's version still being live under the same owner, which also fixes its `to`.
What the fire recorded is kept in `outcome`, for the step to end with.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from daimon.core.config import Settings
from daimon.core.fire.store import Fence, FireResult
from daimon.core.fire.subject import Subject, workflow_subject
from daimon.core.ops_catalog import reason_label
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.routine_delivery import failure_notice_text
from daimon.core.routine_files import plain_payload
from daimon.core.routine_sandbox import RoutineSandbox, routine_sandbox
from daimon.core.stores import (
    identity,
    tenants,
    workflow_runs,
    workflow_triggers,
    workflows,
)
from daimon.core.stores.lead_intake import workflow_execution
from daimon.core.stores.routines import FAILURE_NOTICE
from daimon.core.stores.workflow_run_events import NewEvent, append_events
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.turn.termination import TerminationReason
from daimon.core.workflows import delivery
from daimon.core.workflows.engine import Event
from daimon.core.workflows.spec import RoutineNode, parse_spec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass
class StepOutcome:
    """What the fire recorded; `stale` when the workflow changed under it."""

    status: Literal["ok", "failed", "stale"]
    tail: str | None = None
    error: str | None = None
    delivery_id: uuid.UUID | None = None
    events: list[Event] = field(default_factory=list[Event])


class WorkflowFireStore:
    """`FireStore` for one routine step of `run`, as `workflow` was when the step started."""

    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        subject: Subject,
        workflow: WorkflowRow,
        run: RunRow,
        step_id: uuid.UUID,
    ) -> None:
        self._sm, self._subject, self._workflow, self._run = sm, subject, workflow, run
        self._step_id = step_id
        self.outcome: StepOutcome | None = None
        self.execution_id: uuid.UUID | None = None

    @asynccontextmanager
    async def _granted(self, lifetime_s: float) -> AsyncIterator[None]:
        run = self._run
        async with workflow_execution(
            self._sm, run.tenant_id, run.id, lifetime_s=lifetime_s
        ) as identity:
            self.execution_id = identity
            yield

    def execution(self, lifetime_s: float) -> AbstractAsyncContextManager[None]:
        return self._granted(lifetime_s)

    async def _current(self, session: AsyncSession) -> bool:
        live = await workflows.is_live(
            session,
            workflow_id=self._workflow.id,
            version=self._run.version,
            owner_account_id=self._workflow.owner_account_id,
        )
        if not live:
            self.outcome = StepOutcome("stale")
        return live

    async def _starts_streak(self, session: AsyncSession) -> bool:
        if self._run.mode != "live":
            return False
        ended = await workflow_runs.previous_end(
            session, workflow_id=self._workflow.id, before=self._run.id
        )
        if ended is None:
            last = (self._workflow.converted_from or {}).get("last") or {}
            return last.get("status") != "failed"
        return ended == "succeeded"

    async def _queue(
        self,
        session: AsyncSession,
        outcome: StepOutcome,
        text: str,
        *,
        extra: dict[str, Any] | None = None,
        status: Literal["pending", "skipped"] = "pending",
        note: str | None = None,
    ) -> None:
        s = self._subject
        outcome.delivery_id, outcome.events = await delivery.queue_post(
            session,
            run=self._run,
            workflow=self._workflow,
            kind="result",
            targets=[{"kind": s.destination_kind, "id": s.destination_id}],
            text=text,
            files=[],
            speaker=self._workflow.owner_account_id,
            step_id=self._step_id,
            # Who the post is for and by: the poster reads no routine row.
            extra={"agent_id": s.agent_id, "owner": s.created_by_user_id, **(extra or {})},
            status=status,
            note=note,
        )
        await append_events(
            session,
            run_id=self._run.id,
            events=[NewEvent(e.type, e.step, dict(e.data)) for e in outcome.events],
            actor="engine",
            at=datetime.now(UTC),
        )

    async def _tell_owner(self, session: AsyncSession, reason: str, *, channel_told: bool) -> None:
        told = " Its channel was told." if channel_told else ""
        what = (reason_label(reason) or "Unknown").lower()
        await workflow_triggers.add_owner_notice(
            session,
            tenant_id=self._subject.tenant_id,
            workflow_id=self._workflow.id,
            topic="routine_failed",
            text=f'Run {self._run.number} of "{self._workflow.name}" stopped: {what}.{told}',
        )

    async def refuse(
        self,
        session: AsyncSession,
        reason: str,
        *,
        error: str | None = None,
        fault: bool = False,
        tell: bool = True,
        fence: Fence = "claim",  # the live version fixes the destination too
    ) -> None:
        s = self._subject
        (log.warning if fault else log.info)(
            "routine.fire_failed" if fault else "routine.fire_refused",
            tenant_id=str(s.tenant_id),
            workflow_id=str(self._workflow.id),
            agent_id=s.agent_id,
            reason=reason,
        )
        report_ops_event(
            self._sm,
            OpsEvent(
                "routine.fire_failed",
                s.tenant_id,
                reason=reason,
                severity=None if fault else "warning",
                agent_id=s.agent_id,
                workflow_id=self._workflow.id,
                workflow_run_id=self._run.id,
            ),
            logged=False,
        )
        if not await self._current(session):
            return
        first = await self._starts_streak(session)
        self.outcome = StepOutcome("failed", error=error or reason)
        if tell and first:
            await self._tell_owner(session, reason, channel_told=False)

    async def fail(
        self, session: AsyncSession, *, error: str, reason: TerminationReason | None
    ) -> bool:
        if not await self._current(session):
            return False
        first = await self._starts_streak(session)
        outcome = self.outcome = StepOutcome("failed", error=error)
        told = False
        if reason is not None and first and self._subject.destination_kind not in (None, "dm"):
            tenant = await tenants.get_tenant(session, self._subject.tenant_id)
            text = failure_notice_text(reason, platform=tenant.platform if tenant else "")
            await self._queue(session, outcome, text, note=FAILURE_NOTICE)
            told = True
        if reason is not None and first:
            await self._tell_owner(session, reason, channel_told=told)
        return told

    def tell_owner(self, reason: str | None, *, channel_told: bool) -> None:
        """Already queued with the failure, in its transaction."""

    async def pause_for_lapsed_role(self, session: AsyncSession, *, error: str) -> bool:
        if not await self._current(session):
            return False
        self.outcome = StepOutcome("failed", error=error)
        return await workflows.pause_active(session, workflow_id=self._workflow.id)

    async def lapsed_role_card(self) -> tuple[str, str]:
        return (
            f'Your workflow "{self._workflow.name}" ({self._subject.agent_name}) is paused: '
            "you no longer have a role its agent needs.",
            "Ask an admin for the role, then resume it from its workflow page.",
        )

    async def heal_agent_id(self, agent_id: str) -> bool:
        """Nothing is stored: the agent the fire resolved is the one its result names."""
        self._subject = replace(self._subject, agent_id=agent_id)
        return True

    async def sandbox(self, settings: Settings, *, platform: str) -> RoutineSandbox | None:
        return await routine_sandbox(
            self._sm, settings, self._subject, platform=platform, workflow_id=self._workflow.id
        )

    async def finish(self, result: FireResult) -> None:
        s = self._subject
        status = "pending" if s.destination_kind == "dm" else result.delivery
        async with self._sm() as session, session.begin():
            if not await self._current(session):
                return
            outcome = self.outcome = StepOutcome("ok", tail=result.tail)
            if s.destination_kind is None or status is None:
                return
            extra = {"hold": result.hold, "agent_id": result.producing_agent_id or s.agent_id}
            # A quiet (`NO_POST`) reply queues nothing to post, which delivery skips.
            text = plain_payload(result.tail) if result.payload is None else result.payload
            await self._queue(
                session,
                outcome,
                text or "",
                extra=extra,
                status=status,
                note=result.note,
            )


@dataclass(frozen=True)
class BoundStep:
    """The routine step a credential's signed workflow run names: its workflow as it is now,
    and the step's fire input as of the run's version."""

    workflow: WorkflowRow
    subject: Subject


async def bound_step(
    session: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID, agent_id: str = ""
) -> BoundStep | None:
    """The routine step of `run_id`, read as a routine-bound credential reads its routine row.

    None for a run of another tenant, one with no routine step, or a workflow since deleted
    or changed (the run's version is no longer live). `agent_id` is the step's agent when
    the caller knows it; the owner is read live, so a handed-over workflow names its new owner.
    """
    run = await workflow_runs.get_run(session, run_id, tenant_id=tenant_id)
    if run is None:
        return None
    flow = await workflows.get_workflow(session, tenant_id=tenant_id, workflow_id=run.workflow_id)
    version = await workflows.get_version(
        session, tenant_id=tenant_id, workflow_id=run.workflow_id, number=run.version
    )
    tenant = await tenants.get_tenant(session, tenant_id)
    if flow is None or version is None or tenant is None or flow.live_version != run.version:
        return None
    spec = parse_spec(version.spec)
    node = next((n for n in spec.nodes.values() if isinstance(n, RoutineNode)), None)
    if node is None:
        return None
    owner = flow.owner_account_id
    ids = (
        await identity.platform_external_ids(
            session, tenant_id=tenant_id, platform=tenant.platform, account_ids=[owner]
        )
        if owner
        else {}
    )
    subject = workflow_subject(
        flow,
        spec,
        node,
        run_id=run.id,
        owner=ids.get(owner) if owner else None,
        agent_id=agent_id,
        revision=version.published_at.isoformat(),
    )
    return BoundStep(flow, subject)
