"""The Code step executor: the author's script runs in an isolated worker, JSON in and out.

The worker (the long-job worker pattern) runs one fixed command: a wrapper that writes the
script and `input.json`, runs the script under its timeout, and writes the script's
`output.json` with the attempt's nonce into a result file. The engine trusts only that
file, checks the nonce and validates the output against the node's contract.
"""

from __future__ import annotations

import base64
import json
import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from daimon.core.stores import workflow_runs
from daimon.core.workflows import engine
from daimon.core.workflows.brief import check_output
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.runs import Sessions, StepWork
from daimon.core.workflows.spec import CodeNode, parse_duration
from pydantic import JsonValue

DEFAULT_TIMEOUT_S: Final = 600
MAX_TIMEOUT_S: Final = 3600
RESULT_LIMIT: Final = 64 * 1024
_TAIL: Final = 4000


def result_name(nonce: str) -> str:
    """The result file the wrapper writes to the session's outputs."""
    return f"workflow-code-{nonce}.json"


def code_command(source: str, inputs: JsonValue, nonce: str, timeout_s: int) -> str:
    """The fixed shell command: script and inputs travel as base64 data, never as shell."""

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    script, given = b64(source.encode()), b64(json.dumps(inputs).encode())
    program = f"""import base64, json, pathlib, subprocess, sys
d = pathlib.Path("/tmp/workflow-code-{nonce}")
d.mkdir(parents=True, exist_ok=True)
(d / "main.py").write_bytes(base64.b64decode("{script}"))
(d / "input.json").write_bytes(base64.b64decode("{given}"))
try:
    r = subprocess.run([sys.executable, "main.py"], cwd=d, capture_output=True, timeout={timeout_s})
    code, out = r.returncode, r.stdout + r.stderr
except subprocess.TimeoutExpired as e:
    code, out = None, (e.stdout or b"") + b"\\nThe script ran past its timeout"
try:
    output = json.loads((d / "output.json").read_text())
except (OSError, ValueError):
    output = None
tail = out.decode("utf-8", "replace")[-{_TAIL}:]
print(tail)
result = dict(nonce="{nonce}", exit_code=code, stdout=tail, output=output)
pathlib.Path("/mnt/session/outputs/{result_name(nonce)}").write_text(json.dumps(result))
"""
    return "python3 -c " + shlex.quote(program)


def read_result(raw: bytes | None, nonce: str, node: CodeNode) -> engine.Outcome:
    """The step's outcome from the result file; anything off is never trusted as output."""
    if raw is None or len(raw) > RESULT_LIMIT:
        return engine.Failed("retryable", "The code step left no result")
    try:
        parsed: JsonValue = json.loads(raw)
    except ValueError:
        return engine.Failed("contract", "The code result is not JSON")
    data = parsed if isinstance(parsed, dict) else {}
    if data.get("nonce") != nonce:
        return engine.Failed("contract", "The code result is not from this attempt")
    tail = str(data.get("stdout") or "")[-_TAIL:]
    code = data.get("exit_code")
    if code is None:
        return engine.Failed("fatal", f"The script ran past its timeout. {tail}".strip())
    if code != 0:
        return engine.Failed("fatal", f"The script exited with code {code}. {tail}".strip())
    output = data.get("output")
    if not isinstance(output, dict):
        return engine.Failed("contract", "The script wrote no output.json object")
    if node.output is not None:
        filled, errors = check_output(node.output, output)
        if errors:
            return engine.Failed("contract", "; ".join(errors))
        output = filled
    return engine.Succeeded(output, summary=tail or None)


@dataclass(frozen=True)
class CodeJob:
    run_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID
    command: str
    nonce: str
    timeout_s: int
    network: bool


class CodeWorkerFailed(Exception):
    """The worker could not run the script; the message says why."""


class CodeWorker(Protocol):
    async def __call__(self, work: StepWork, job: CodeJob, /) -> bytes | None:
        """Run `job.command` in an isolated worker; the raw result file, or None."""
        ...


@dataclass(frozen=True)
class CodeStepExecutor:
    sm: Sessions
    worker: CodeWorker
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def __call__(self, work: StepWork) -> engine.Outcome:
        node = work.node
        assert isinstance(node, CodeNode), "the code executor runs code nodes"
        try:
            inputs = render(dict(node.inputs or {}), work.context, now=self.clock())
        except ExpressionError as err:
            return engine.Failed("fatal", str(err))
        timeout = parse_duration(node.timeout).total_seconds() if node.timeout else 0
        timeout_s = min(int(timeout) or DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
        network = node.network == "on"
        async with self.sm.begin() as s:
            attempt = await workflow_runs.running_attempt(s, work.step.id)
            assert attempt is not None, "a claimed step has a running attempt"
            # Per attempt, and the same when a lost worker's attempt reattaches.
            nonce = attempt.id.hex
            if network:
                # A script with network may write outside: a lost worker is `unknown`.
                await workflow_runs.mark_send_intent(s, step_id=work.step.id, now=self.clock())
        job = CodeJob(
            work.run.id,
            work.step.id,
            attempt.id,
            code_command(node.source, inputs, nonce, timeout_s),
            nonce,
            timeout_s,
            network,
        )
        try:
            raw = await self.worker(work, job)
        except CodeWorkerFailed as err:
            return engine.Failed("fatal", str(err))
        return read_result(raw, nonce, node)
