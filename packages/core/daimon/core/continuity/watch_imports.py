"""Versioned, offline watch import manifest. Never exports or accepts secrets."""

from datetime import datetime
from typing import Annotated, Literal

from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.poll_tools import require_poll_tool
from pydantic import BaseModel, ConfigDict, Field, JsonValue

UNMAPPED_NOTICE = "this watch is paused: its source can't be polled here"


class ImportedWatch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    legacy_id: str = Field(min_length=1, max_length=128)
    os_binding: ContinuationRequest
    source: Literal["page_event", "poll"]
    instructions: str = Field(min_length=1, max_length=2000)
    page_id: str | None = None
    tool: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    projection: str = Field(default=".", max_length=2000)
    rule: Literal["change", "added", "updated"] = "change"
    interval: int = Field(default=600, ge=60, le=86400)
    baseline: dict[str, JsonValue] = Field(default_factory=dict)
    expires_at: datetime
    max_turns_per_day: int = Field(default=24, ge=1, le=120)


PlaceType = Literal["direct-message", "thread", "channel", "mcp", "voice", "routine"]


class ImportedCallbackWatch(BaseModel):
    """A watch fed by posts to a secret link. Only the token's sha256 is carried."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    legacy_id: str = Field(min_length=1, max_length=128)
    source: Literal["callback"]
    instructions: str = Field(min_length=1, max_length=2000)
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime | None = None
    owner_discord_id: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=64)
    thread_id: str | None = Field(default=None, max_length=64)
    place_type: PlaceType


class ImportedTimer(BaseModel):
    """A one-shot timer, held until confirmed for an agent."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    source_id: str = Field(min_length=1, max_length=128)
    owner_discord_id: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=64)
    thread_id: str | None = Field(default=None, max_length=64)
    place_type: PlaceType
    fire_at: datetime
    note: str = Field(min_length=1, max_length=2000)


class WatchExport(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal[1]
    watches: list[
        Annotated[ImportedWatch | ImportedCallbackWatch, Field(discriminator="source")]
    ] = Field(max_length=25)
    timers: list[ImportedTimer] = Field(default_factory=list[ImportedTimer], max_length=1000)


def map_poll_source(name: str, arguments: dict[str, JsonValue]) -> tuple[str, dict[str, JsonValue]]:
    """Translate only representable source arguments; native validation still applies."""
    if name == "discord_read_messages":
        if set(arguments) - {"channel_or_thread_id", "limit"}:
            raise ValueError("Unsupported imported message-read arguments")
        limit = arguments.get("limit", 20)
        if type(limit) is not int:
            raise ValueError("Imported message-read limit must be an integer")
        name = "read_thread"
        arguments = {
            "thread_id": arguments.get("channel_or_thread_id"),
            "limit": min(limit, 20),
        }
    elif name == "discord_read_channel_since":
        if set(arguments) - {"channel_id", "since"}:
            raise ValueError("Unsupported imported channel-history arguments (including paging)")
        name = "read_channel"
        # Native polling diffs its saved baseline; since is not a live-read filter.
        # This maps channel messages only, not archived history or child threads.
        arguments = {key: value for key, value in arguments.items() if key != "since"}
        channel_id = arguments.get("channel_id")
        if isinstance(channel_id, str) and channel_id.startswith("discord_channel:"):
            arguments["channel_id"] = channel_id.removeprefix("discord_channel:")
    require_poll_tool(name)
    return name, arguments
