"""Code steps: the fixed wrapper, the result check, the feature gate and the worker stamps."""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from daimon.core.config import Settings
from daimon.core.stores import workflow_runs
from daimon.core.workflows import engine, runs
from daimon.core.workflows.code_step import (
    CodeJob,
    CodeStepExecutor,
    code_command,
    read_result,
    result_name,
)
from daimon.core.workflows.code_worker import ManagedCodeWorker
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.spec import CodeNode
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.workflows import NOW, make_world
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    json_body,
    list_response,
)
from mux.testing.drivers.anthropic.ma_models import ma_session
from sqlalchemy.ext.asyncio import AsyncSession

SCRIPT = """import json
data = json.load(open("input.json"))
print("adding")
json.dump({"total": sum(data["xs"])}, open("output.json", "w"))
"""
NODE = CodeNode.model_validate(
    {"kind": "code", "source": SCRIPT, "output": {"total": {"type": "number"}}}
)
CODE: dict[str, Any] = {
    "c": {"kind": "code", "source": SCRIPT, "inputs": {"xs": "${{ inputs.xs }}"}, "next": "f"},
    "f": {"kind": "finish", "output": "${{ steps.c.output }}"},
}


def code_on(settings: Settings) -> Settings:
    return settings.model_copy(update={"features": {"workflows": "on", "workflow_code": "on"}})


def run_wrapper(command: str, outputs: Path) -> bytes:
    """The worker's job, here: the fixed command with the session outputs in a temp dir."""
    subprocess.run(command.replace("/mnt/session/outputs", str(outputs)), shell=True, check=True)
    return next(outputs.iterdir()).read_bytes()


def test_the_wrapper_runs_the_script_and_stamps_the_nonce(tmp_path: Path) -> None:
    nonce = uuid.uuid4().hex
    raw = run_wrapper(code_command(SCRIPT, {"xs": [1, 2]}, nonce, 30), tmp_path)
    assert (tmp_path / result_name(nonce)).exists(), "the result file is named by the nonce"
    done = read_result(raw, nonce, NODE)
    assert done == engine.Succeeded({"total": 3}, summary="adding\n"), f"{done}"


def test_a_result_from_another_attempt_or_a_bad_exit_is_not_trusted() -> None:
    good = {"nonce": "n1", "exit_code": 0, "stdout": "", "output": {"total": 1}}

    def outcome(**change: Any) -> engine.Outcome:
        return read_result(json.dumps(good | change).encode(), "n1", NODE)

    assert outcome(nonce="n0") == engine.Failed(
        "contract", "The code result is not from this attempt"
    )
    assert outcome(exit_code=2, stdout="boom") == engine.Failed(
        "fatal", "The script exited with code 2. boom"
    )
    assert outcome(output={"total": "x"}) == engine.Failed(
        "contract", "output.total must be a number"
    ), "the contract is checked"
    assert read_result(None, "n1", NODE) == engine.Failed(
        "retryable", "The code step left no result"
    ), "a lost result retries"


async def test_code_steps_are_refused_while_the_feature_is_off(db_session: AsyncSession) -> None:
    world = await make_world(db_session, CODE, "c")
    refused = await runs.start_run(
        world.sm, world.settings, tenant_id=world.tenant_id, start=world.start(), now=NOW
    )
    assert refused == Refusal(
        "code_off", "Code steps are off here. Ask an admin to turn them on."
    ), f"{refused}"


async def test_the_executor_renders_inputs_and_checks_its_attempts_nonce(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, CODE, "c")
    world.settings = code_on(world.settings)
    run = await world.run(inputs={"xs": [2, 5]})
    jobs: list[CodeJob] = []

    async def worker(_work: runs.StepWork, job: CodeJob) -> bytes:
        jobs.append(job)
        output = {"nonce": job.nonce, "exit_code": 0, "stdout": "ok", "output": {"total": 7}}
        return json.dumps(output).encode()

    executor = CodeStepExecutor(world.sm, worker, clock=lambda: NOW)
    claimed = await world.claim(run.id, "c")
    await runs.execute(world.sm, world.settings, {"code": executor}, claimed, clock=lambda: NOW)
    done = await world.get(run.id)
    assert done.status == "succeeded" and (await world.steps(run.id))["f"].output == {"total": 7}
    [job] = jobs
    assert '"xs": [2, 5]' not in job.command, "inputs travel encoded, never as shell text"
    assert (job.run_id, job.step_id, job.network) == (run.id, claimed.step.id, False)


async def test_a_lost_worker_with_no_saved_session_keeps_its_attempt(
    db_session: AsyncSession,
) -> None:
    """Its worker is found again by attempt, so a retry never leaves the first one behind."""
    world = await make_world(db_session, CODE, "c")
    world.settings = code_on(world.settings)
    run = await world.run(inputs={"xs": [1]})
    claimed = await world.claim(run.id, "c")
    async with world.sm() as s:
        first = await workflow_runs.running_attempt(s, claimed.step.id)
    await runs.reclaim(
        world.sm, world.settings, step_id=claimed.step.id, now=NOW + timedelta(minutes=10)
    )
    again = await world.claim(run.id, "c")
    async with world.sm() as s:
        attempts = await workflow_runs.list_attempts(s, again.step.id)
    assert first is not None and [a.id for a in attempts] == [first.id], attempts


async def test_the_worker_is_stamped_for_the_run_step_and_person(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, CODE, "c")
    world.settings = code_on(world.settings)
    run = await world.run(inputs={"xs": [1]})
    claimed = await world.claim(run.id, "c")
    router = MARouter()
    agent, env = ma_agent(id="agent_1", version=3), ma_environment(id="env_1")
    router.add_agent(agent)
    router.add_environment(env)
    router.add("GET", r"/v1/sessions$", lambda _r, _m: list_response([]))
    created: list[dict[str, Any]] = []

    def create(request: httpx.Request, _match: object) -> httpx.Response:
        created.append(body := json_body(request))
        session = ma_session(
            id="ses_1", agent=agent, environment_id=env.id, metadata=body["metadata"]
        )
        return httpx.Response(200, json=session.model_dump(mode="json"))

    router.add("POST", r"/v1/sessions$", create)
    client = build_fake_anthropic(router.dispatch)

    async def targets(_work: runs.StepWork) -> tuple[str, str]:
        return "agent_1", "env_1"

    try:
        worker = ManagedCodeWorker(client, world.sm, world.settings, targets, clock=lambda: NOW)
        async with world.sm() as s:
            spec = await runs.spec_of(s, run)
        work = runs.StepWork(run, claimed.step, spec.nodes["c"], spec, 1, {})
        job = CodeJob(run.id, claimed.step.id, uuid.uuid4(), "true", "n", 60, False)
        assert await worker.port().create_or_find(await worker.long_job(work, job)) == "ses_1"
    finally:
        await client.close()
    meta = created[0]["metadata"]
    assert meta["daimon_workflow_run"] == str(run.id), "usage rolls up to the run"
    assert meta["daimon_workflow_step"] == str(claimed.step.id), "and to the step"
    assert meta["daimon_account"] == str(world.owner), "metered to the run-as person"
    assert "daimon_budget_channel" not in meta, "no home channel, no channel budget"
