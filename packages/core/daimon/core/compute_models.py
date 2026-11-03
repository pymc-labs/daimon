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
    request_key: str
    app_name: str
    profile: str
    image: str
    region: str
    cpu_kind: Literal["shared", "performance"]
    cpus: int
    memory_mb: int
    state: Literal["launching", "running", "stopping", "deleted"] = "launching"
    machine_id: str | None = None
    ttl_hours: int
    expires_at: datetime
    created_at: datetime
    warned_for: datetime | None = None


class ComputeStatus(ComputeSession):
    machine_state: str


class ComputeResource(BaseModel):
    app_name: str
    deployment_id: str
    session_id: uuid.UUID
    machine_state: str = "unknown"
    machine_id: str | None = None


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
