"""Owned ephemeral compute records; provider credentials never enter these models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ComputeSession(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    owner_account_id: uuid.UUID
    owner_external_user_id: str
    deployment_id: str
    platform: str
    parent_channel_id: str
    thread_id: str
    agent_id: str
    agent_name: str
    source_metadata: dict[str, str] = Field(default_factory=dict)
    extension_keys: list[str] = Field(default_factory=list)
    # The wake turns (origin ids, hex) that acted since the owner last ran a command or
    # wrote a file here. Changed only under the session lock (`turn_may_act`).
    wake_turns: list[str] = Field(default_factory=list)
    request_key: str
    app_name: str
    profile: str
    image: str
    region: str
    cpus: int
    memory_mb: int
    state: Literal["launching", "running", "stopping", "deleted"] = "launching"
    machine_id: str | None = None
    ttl_hours: int
    expires_at: datetime
    created_at: datetime
    warned_for: datetime | None = None


#: Every state a provider may report for a session's machine:
#: `starting` it exists but cannot answer a call yet; `started` it answers calls;
#: `failed` it will never start as it is; `destroying` it is being removed;
#: `no_machines` the provider holds nothing for the session; `unknown` not read.
MachineState = Literal["starting", "started", "failed", "destroying", "no_machines", "unknown"]
MachineReadiness = Literal["ready", "starting", "failed", "gone"]


def machine_readiness(machine_state: str) -> MachineReadiness:
    """What a machine state means for using the session: the one place that decides.

    Only `started` is ready. A state this does not know, from a provider that says
    more than `MachineState`, counts as starting: a machine is never taken for ready
    by accident.
    """
    if machine_state == "started":
        return "ready"
    if machine_state == "failed":
        return "failed"
    if machine_state in ("destroying", "no_machines"):
        return "gone"
    return "starting"


class ComputeStatus(ComputeSession):
    machine_state: MachineState


class ComputeResource(BaseModel):
    app_name: str
    deployment_id: str
    session_id: uuid.UUID
    machine_state: MachineState = "unknown"
    machine_id: str | None = None
    # Where the provider holds it, when known: a Cloud Run service is addressed by region.
    region: str | None = None


ComputeTaskState = Literal[
    "starting", "running", "succeeded", "failed", "timed_out", "lost", "cancelled"
]
ACTIVE_TASK_STATES: frozenset[str] = frozenset({"starting", "running"})


class ComputeTask(BaseModel):
    """A detached command on a session; its conversation is the session's own."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    owner_account_id: uuid.UUID
    request_key: str
    command_preview: str
    state: ComputeTaskState = "starting"
    exit_code: int | None = None
    timeout_minutes: int
    deadline_at: datetime
    output_bytes: int = 0
    output_tail: str = ""
    output_path: str
    created_at: datetime
    finished_at: datetime | None = None
    version: int = 0
    # While the caller of an inline command still waits for it, the scheduler leaves it.
    wait_until: datetime | None = None
