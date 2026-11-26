"""In-memory compute provider for lifecycle and isolation tests, with no network I/O."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.compute_models import ComputeResource, ComputeSession, MachineState
from daimon.core.compute_provider import ComputeProviderError
from pydantic import JsonValue


@dataclass
class FakeTask:
    """What a machine's task files would say; tests move it along by hand."""

    command: str
    status: str = "running"
    exit_code: int | None = None
    output: str = ""
    starts: int = 1


class FakeComputeProvider:
    """A provider whose machines start at once, unless a test says otherwise.

    `starts_after` is how many times a machine is read as `starting` before it has
    started, and `fail_start` makes it `failed` instead. A call to a machine that has
    not started fails, as a real one does.
    """

    def __init__(self) -> None:
        self.starts_after = 0
        self.fail_start = False
        self.reads: dict[uuid.UUID, int] = {}
        self.tasks: dict[str, FakeTask] = {}
        self.fail_calls = False
        self.resources: dict[uuid.UUID, ComputeResource] = {}
        self.machine_env: dict[uuid.UUID, dict[str, str]] = {}
        self.calls: list[tuple[uuid.UUID, str, dict[str, JsonValue]]] = []
        self.deleted: list[uuid.UUID] = []
        self.deleted_regions: list[str | None] = []
        self.fail_create = False
        self.fail_delete = False
        self.result: JsonValue = {"exit_code": 0, "stdout": "done", "timed_out": False}

    async def ensure(self, session: ComputeSession, token: str) -> ComputeResource:
        resource = self.resources.setdefault(
            session.id,
            ComputeResource(
                app_name=session.app_name,
                deployment_id=session.deployment_id,
                session_id=session.id,
            ),
        )
        if self.fail_create:
            raise ComputeProviderError("Simulated partial create")
        self.machine_env[session.id] = {"MCP_AUTH_TOKEN": token}
        return self._read(resource.model_copy(update={"machine_id": "machine-" + session.id.hex}))

    def _read(self, resource: ComputeResource) -> ComputeResource:
        """One look at the machine: each look brings a starting machine closer to started."""
        seen = self.reads.get(resource.session_id, 0)
        self.reads[resource.session_id] = seen + 1
        state: MachineState = (
            "failed" if self.fail_start else "started" if seen >= self.starts_after else "starting"
        )
        resource = resource.model_copy(update={"machine_state": state})
        self.resources[resource.session_id] = resource
        return resource

    async def inspect(self, session: ComputeSession) -> ComputeResource:
        resource = self.resources.get(session.id)
        if resource is None:
            return ComputeResource(
                session_id=session.id,
                app_name=session.app_name,
                deployment_id=session.deployment_id,
                machine_state="no_machines",
            )
        return self._read(resource)

    async def delete(self, resource: ComputeResource) -> None:
        if self.fail_delete:
            raise ComputeProviderError("Simulated failed deletion")
        self.resources.pop(resource.session_id, None)
        self.machine_env.pop(resource.session_id, None)
        self.deleted.append(resource.session_id)
        self.deleted_regions.append(resource.region)

    async def inventory(self) -> list[ComputeResource]:
        return list(self.resources.values())

    async def call(
        self, session: ComputeSession, token: str, tool: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        if self.machine_env.get(session.id) != {"MCP_AUTH_TOKEN": token}:
            raise ComputeProviderError("Invalid machine token")
        if self.resources[session.id].machine_state != "started":
            raise ComputeProviderError("Simulated machine that has not started")
        if self.fail_calls:
            raise ComputeProviderError("Simulated unreachable machine")
        self.calls.append((session.id, tool, arguments))
        command = arguments.get("command")
        if tool == "run_bash" and isinstance(command, str) and command.startswith("# daimon-task"):
            return self._task(command)
        return self.result

    def _task(self, script: str) -> JsonValue:
        _, _, action, task_hex = script.splitlines()[0].split()
        task = self.tasks.get(task_hex)
        stdout = ""
        if action == "start":
            if task is None:
                self.tasks[task_hex] = FakeTask(command=script)
                stdout = "started\n"
            else:
                task.starts += 1
                stdout = "exists\n"
        elif action == "kill" and task is not None:
            task.status, task.exit_code = "exit", 124
        elif action == "probe":
            status = "missing" if task is None else task.status
            if task is not None and task.status == "exit":
                status = f"exit {task.exit_code}"
            output = "" if task is None else task.output
            stdout = f"{status}\nbytes {len(output.encode())}\n{output[-16384:]}"
        return {"exit_code": 0, "stdout": stdout, "timed_out": False}
