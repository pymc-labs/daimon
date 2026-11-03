"""Public long-job records and bounded tenant policy."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

LongJobState = Literal[
    "queued",
    "launching",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "lost",
    "over_budget",
]
ACTIVE_STATES = ("queued", "launching", "running")

# Keep Decimal arithmetic internally and interoperable dollar strings on MCP.
Money = Annotated[Decimal, WithJsonSchema({"type": "string"})]


class LongJobPolicy(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    enabled: bool = False
    default_deadline_minutes: int = Field(default=60, gt=0)
    max_deadline_minutes: int = Field(default=70, gt=0)
    max_usd: Decimal = Field(default=Decimal("5"), gt=0, allow_inf_nan=False)


class LongJob(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    owner_account_id: uuid.UUID
    owner_external_user_id: str
    agent_id: str
    agent_name: str
    agent_version: int
    environment_id: str
    environment_updated_at: datetime
    platform: str
    parent_channel_id: str
    thread_id: str
    parent_session_id: str
    tool_use_id: str
    source_metadata: dict[str, str] = Field(default_factory=dict)
    initial_send_at: datetime | None = None
    command: str
    title: str
    deadline_minutes: int
    max_usd: Money
    state: LongJobState
    version: int
    worker_session_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    deadline_at: datetime | None = None
    stop_state: LongJobState | None = None
    cancel_requested: bool = False
    interrupt_attempted_at: datetime | None = None
    interrupted_at: datetime | None = None
    archived_at: datetime | None = None
    summary: str | None = None
    exit_code: int | None = None
    output_files: list[str] = Field(default_factory=list)
    cost_usd: Money = Decimal("0")
    created_at: datetime
    finished_at: datetime | None = None


class LongJobManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exit_code: int | None
    summary: str = Field(default="", max_length=4000)
    output_files: list[Annotated[str, Field(max_length=256)]] = Field(
        default_factory=list, max_length=100
    )


class LongJobRedacted(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    state: LongJobState
    owner_account_id: uuid.UUID
    created_at: datetime
    deadline_at: datetime | None
    source: Literal["source restricted"] = "source restricted"


class LongJobCancelReply(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    state: LongJobState
    owner_account_id: uuid.UUID
