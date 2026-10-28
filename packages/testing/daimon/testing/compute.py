"""In-memory compute provider for lifecycle and isolation tests, with no network I/O."""

from __future__ import annotations

import uuid

from daimon.core.compute_models import ComputeResource, ComputeSession
from daimon.core.compute_provider import ComputeProviderError
from pydantic import JsonValue


class FakeComputeProvider:
    def __init__(self) -> None:
        self.resources: dict[uuid.UUID, ComputeResource] = {}
        self.machine_env: dict[uuid.UUID, dict[str, str]] = {}
        self.calls: list[tuple[uuid.UUID, str, dict[str, JsonValue]]] = []
        self.deleted: list[uuid.UUID] = []
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
        resource = resource.model_copy(
            update={"machine_id": "machine-" + session.id.hex, "machine_state": "started"}
        )
        self.resources[session.id] = resource
        self.machine_env[session.id] = {"MCP_AUTH_TOKEN": token}
        return resource

    async def inspect(self, session: ComputeSession) -> ComputeResource:
        return self.resources.get(
            session.id,
            ComputeResource(
                session_id=session.id,
                app_name=session.app_name,
                deployment_id=session.deployment_id,
                machine_state="no_machines",
            ),
        )

    async def delete(self, resource: ComputeResource) -> None:
        if self.fail_delete:
            raise ComputeProviderError("Simulated failed deletion")
        self.resources.pop(resource.session_id, None)
        self.machine_env.pop(resource.session_id, None)
        self.deleted.append(resource.session_id)

    async def inventory(self) -> list[ComputeResource]:
        return list(self.resources.values())

    async def call(
        self, session: ComputeSession, token: str, tool: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        if self.machine_env.get(session.id) != {"MCP_AUTH_TOKEN": token}:
            raise ComputeProviderError("Invalid machine token")
        self.calls.append((session.id, tool, arguments))
        return self.result
