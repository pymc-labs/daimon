"""`TurnState` and its content-block types.

State is a frozen dataclass. Reducers return new instances via
`dataclasses.replace`. `seen_event_ids` is a `frozenset[str]` so equality
and hashing stay well-defined.

Per refinements §5: fields that verbatim-copy an SDK value (`stop_reason`,
`result_content`, `evaluated_permission`) carry the SDK type directly. Only
fields that the reducer constructs by folding/joining events are daimon
types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_result_event import (
    Content as ToolResultContent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    StopReason,
)
from daimon.core.errors import TurnError


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Assistant text. Grown by appending across `agent.message` events —
    the reducer concatenates onto the most recent `TextBlock` when no
    tool-use block has intervened."""

    kind: Literal["text"]
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """A tool invocation observed in the turn.

    `id` is the originating event's id (= the SDK tool_use_id used by the
    matching tool-result event). `type` is the raw SDK event-type string —
    adapters branch on it (`mcp_server_name` presence is the MCP marker).
    """

    kind: Literal["tool_use"]
    id: str
    type: Literal["agent.tool_use", "agent.custom_tool_use", "agent.mcp_tool_use"]
    name: str
    input: dict[str, object]
    evaluated_permission: Literal["allow", "ask", "deny"] | None = None
    mcp_server_name: str | None = None
    status: Literal["pending", "complete", "failed"] = "pending"
    result_content: list[ToolResultContent] | None = None
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock
"""Ordered, discriminated union of blocks inside `TurnState.content`."""


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Per-turn token totals, folded from `span.model_request_end` events.

    Field names match `BetaManagedAgentsSpanModelUsage` verbatim so a usage
    payload can be reconstructed 1:1 and priced through `daimon.core.pricing`
    in the adapter shell. The reducer never prices — it has no model id and
    no rates; it only accumulates the four cache-split stage totals.
    """

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Task:
    """Placeholder for subagent-task tracking. Managed Agents emits no
    `agent.task_*` events in phase 1; this type exists so `TurnState.tasks`
    is typed and forward-compatible."""


@dataclass(frozen=True, slots=True)
class TurnState:
    """Full in-memory state of a single turn — pure fold of `apply` over
    the event stream. Reducers never mutate; they return new instances."""

    content: list[ContentBlock] = field(default_factory=list[ContentBlock])
    tasks: list[Task] = field(default_factory=list[Task])
    rate_limit_until: datetime | None = None
    stop_reason: StopReason | None = None
    error: TurnError | None = None
    seen_event_ids: frozenset[str] = field(default_factory=frozenset[str])
    usage_totals: UsageTotals = field(default_factory=UsageTotals)


def extract_final_response(content: list[ContentBlock]) -> str:
    """Extract text after the last ToolUseBlock, or all text if no tools.

    In multi-tool turns, intermediate agent.message text (pre-tool narration)
    is positionally before ToolUseBlocks. Only text AFTER the last tool use
    is the actual response. When no tools were used, all text is final.
    """
    last_tool_idx = -1
    for i, block in enumerate(content):
        if isinstance(block, ToolUseBlock):
            last_tool_idx = i

    return "".join(
        block.text for block in content[last_tool_idx + 1 :] if isinstance(block, TextBlock)
    )


def extract_sealed_responses(
    content: list[ContentBlock], *, min_chars: int
) -> list[tuple[int, str]]:
    """Text blocks "sealed" by a later tool use and at least ``min_chars`` long.

    A text block with a ToolUseBlock after it can never grow again (the reducer
    only appends onto the last block), so it is safe to render permanently
    mid-turn. The length floor separates substantive answers from pre-tool
    narration ("Let me check…"). Returns ``(content index, text)`` pairs in
    stream order; the index is a stable dedup key for callers that flush
    incrementally. Text after the last tool use is the final response and is
    never included — that's ``extract_final_response``'s job.
    """
    last_tool_idx = -1
    for i, block in enumerate(content):
        if isinstance(block, ToolUseBlock):
            last_tool_idx = i

    return [
        (i, block.text)
        for i, block in enumerate(content[:last_tool_idx])
        if isinstance(block, TextBlock) and len(block.text) >= min_chars
    ]
