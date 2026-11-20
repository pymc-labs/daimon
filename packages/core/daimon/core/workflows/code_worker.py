"""A Code step's worker on Managed Agents: the long-job worker, stamped for the step.

It is the long-job worker session (`ManagedLongJobPort`: built-in tools only, no MCP, no
skills, a fixed command, a budget limit), owned by the run-as person and stamped with the
run and step, so its usage is metered to that person and rolled up to the step. The
attempt saves the session before the first send, so a lost worker's attempt reattaches.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import anthropic
from daimon.core.config import Settings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_WORKFLOW_RUN,
    MA_METADATA_KEY_WORKFLOW_STEP,
)
from daimon.core.long_job_models import LongJob
from daimon.core.long_jobs import WorkerBudgetReached, WorkerVanished
from daimon.core.long_jobs_ma import ManagedLongJobPort
from daimon.core.mux_compat import retrieve_agent, retrieve_environment
from daimon.core.output_delivery import poll_output_files
from daimon.core.output_ports_compat import read_output_record
from daimon.core.stores import workflow_runs
from daimon.core.workflows.code_step import RESULT_LIMIT, CodeJob, CodeWorkerFailed, result_name
from daimon.core.workflows.runs import Sessions, StepWork

#: The worker's own time around the script: launch, waits and the result copy.
_SLACK: Final = timedelta(minutes=5)
#: Agent and environment ids for the step's worker.
Targets = Callable[[StepWork], Awaitable[tuple[str, str]]]


@dataclass(frozen=True)
class ManagedCodeWorker:
    client: anthropic.AsyncAnthropic
    sm: Sessions
    settings: Settings
    targets: Targets
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, work: StepWork, job: CodeJob) -> bytes | None:
        port = self.port()
        long_job = await self.long_job(work, job)
        try:
            session_id = await port.create_or_find(long_job)
            async with self.sm.begin() as s:
                await workflow_runs.set_attempt_session(
                    s, attempt_id=job.attempt_id, session_id=session_id, now=self.clock()
                )
            long_job = long_job.model_copy(update={"worker_session_id": session_id})
            await port.drive(
                long_job, resume=await port.has_started(long_job), send_guard=nullcontext
            )
            raw = await self._result(port, long_job, job)
        except WorkerBudgetReached as err:
            raise CodeWorkerFailed("The code step reached its budget") from err
        except WorkerVanished as err:
            raise CodeWorkerFailed(str(err) or "The code worker went away") from err
        finally:
            if long_job.worker_session_id is not None:
                await port.settle(long_job)
                await port.archive(long_job)
        return raw

    def port(self) -> ManagedLongJobPort:
        return ManagedLongJobPort(self.client, self.sm, self.settings)

    async def long_job(self, work: StepWork, job: CodeJob) -> LongJob:
        """The step's worker as a long job: the run-as person's, stamped with run and step."""
        run = work.run
        assert run.run_as_account_id is not None, "preflight clears the run-as person"
        agent_id, environment_id = await self.targets(work)
        now = self.clock()
        draft = LongJob(
            id=job.attempt_id,
            tenant_id=run.tenant_id,
            owner_account_id=run.run_as_account_id,
            owner_external_user_id="",
            agent_id=agent_id,
            agent_name="",
            agent_version=0,
            environment_id=environment_id,
            environment_updated_at=now,
            platform="workflow",
            parent_channel_id=(work.place.workflow.home_channel_id or "") if work.place else "",
            thread_id="",
            parent_session_id="",
            tool_use_id="",
            source_metadata={
                MA_METADATA_KEY_WORKFLOW_RUN: str(job.run_id),
                MA_METADATA_KEY_WORKFLOW_STEP: str(job.step_id),
            },
            command=job.command,
            title="Workflow code step",
            deadline_minutes=math.ceil((job.timeout_s + _SLACK.total_seconds()) / 60),
            max_usd=max(run.budget_usd - run.cost_usd, Decimal("0.01")),
            state="running",
            version=1,
            deadline_at=now + timedelta(seconds=job.timeout_s) + _SLACK,
            created_at=now,
        )
        scope = self.port().scope(draft)
        agent = await retrieve_agent(self.client, agent_id, scope=scope)
        env = await retrieve_environment(self.client, environment_id, scope=scope)
        return draft.model_copy(
            update={
                "agent_name": agent.name,
                "agent_version": agent.version,
                "environment_updated_at": datetime.fromisoformat(env.updated_at),
            }
        )

    async def _result(
        self, port: ManagedLongJobPort, long_job: LongJob, job: CodeJob
    ) -> bytes | None:
        assert long_job.worker_session_id is not None
        scope = port.scope(long_job)
        files = await poll_output_files(
            self.client, session_id=long_job.worker_session_id, sleep=asyncio.sleep, scope=scope
        )
        name = result_name(job.nonce)
        for file in files.values():
            if file.filename == name and file.size_bytes <= RESULT_LIMIT:
                return await read_output_record(self.client, file.id, scope=scope)
        return None
