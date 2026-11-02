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

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, cast

from anthropic.types.beta.sessions.beta_managed_agents_agent_tool_result_event import (
    Content as ToolResultContent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    StopReason,
)
from daimon.core.errors import TurnError
from daimon.core.pricing import uncached_input_tokens
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.status_log import status_log_enabled
from daimon.core.turn.termination import TerminationReason
from mux.contracts.usage import UsageObservation
from mux.profiles import accounting_mode_for


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Assistant text. Managed Agents delivers each `agent.message` whole (its
    streamed deltas are previews that never reach the reducer). With
    `turn.status_log` on, every message is its own block; off, the reducer
    concatenates consecutive messages onto the most recent `TextBlock` when no
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

#: The MCP search interface's proxy: `call_tool(name=..., arguments=...)`.
_CALL_TOOL: Final[str] = "call_tool"


def daimon_tool_arguments(block: ToolUseBlock, tool: str) -> Mapping[str, object] | None:
    """The arguments of a successful call to daimon's own MCP tool `tool`, or None.

    Recognises the direct call and the same call made through the MCP search
    interface (`call_tool(name=tool, arguments={...})`), which is how a large
    daimon catalog is reached.
    """
    if block.status != "complete" or block.is_error:
        return None
    return daimon_tool_input(block, tool)


def daimon_tool_input(block: ToolUseBlock, tool: str) -> Mapping[str, object] | None:
    """`daimon_tool_arguments` for a call that may still be running; None once it failed."""
    if block.mcp_server_name != DAIMON_SERVER_NAME or block.status == "failed" or block.is_error:
        return None
    arguments: object = block.input
    if block.name == _CALL_TOOL:
        if block.input.get("name") != tool:
            return None
        arguments = block.input.get("arguments") or {}
    elif block.name != tool:
        return None
    return cast("dict[str, object]", arguments) if isinstance(arguments, dict) else None


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Per-turn token totals, folded from `span.model_request_end` events.

    Field names match `BetaManagedAgentsSpanModelUsage` verbatim for pricing.
    The reducer only accumulates the four cache-split stage totals; it has no
    model id or rates.
    """

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    def add_observation(self, usage: UsageObservation) -> UsageTotals:
        """Sum reported request stages; unknown buckets add no measurement."""
        if accounting_mode_for(usage.session.provider).settles_natively:
            from daimon.core.usage_totals import ProviderUsageTotals

            if any(
                (
                    self.input_tokens,
                    self.output_tokens,
                    self.cache_creation_input_tokens,
                    self.cache_read_input_tokens,
                )
            ):
                raise ValueError("cannot mix legacy and alternate-provider usage totals")
            return ProviderUsageTotals().add_observation(usage)
        if usage.grain != "model_request" or usage.basis != "increment":
            raise ValueError("turn totals require disjoint model-request increments")
        uncached = uncached_input_tokens(usage)
        return UsageTotals(
            input_tokens=self.input_tokens + (uncached if uncached is not None else 0),
            output_tokens=self.output_tokens
            + (usage.output_tokens if usage.output_tokens is not None else 0),
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + (usage.input_cache_write_tokens if usage.input_cache_write_tokens is not None else 0),
            cache_read_input_tokens=self.cache_read_input_tokens
            + (usage.input_cached_tokens if usage.input_cached_tokens is not None else 0),
        )


@dataclass(frozen=True, slots=True)
class McpServerFailure:
    """One MCP server MA could not use this turn, folded from `session.error`.

    Kept apart from `TurnState.error` on purpose: MA keeps the session running
    without the server (the connection is retried on the next turn), so a
    failure here degrades the turn instead of ending it. `retry_status` is the
    SDK discriminator verbatim; `terminal` never lands here because the
    reducer routes that one to `error`.
    """

    server_name: str
    error_type: Literal["mcp_connection_failed_error", "mcp_authentication_failed_error"]
    message: str
    retry_status: Literal["retrying", "exhausted"]


@dataclass(frozen=True, slots=True)
class Task:
    """Placeholder for subagent-task tracking. Managed Agents emits no
    `agent.task_*` events currently; this type exists so `TurnState.tasks`
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
    long_prompt_usage_totals: UsageTotals = field(default_factory=UsageTotals)
    """Subset of usage_totals from requests over the long-context prompt threshold."""
    mcp_failures: tuple[McpServerFailure, ...] = ()
    """Servers that failed this turn, newest status per server name."""
    retrying_error: TurnError | None = None
    """The last `retrying` error MA reported; only surfaces if nothing follows."""
    package_install_error: TurnError | None = None
    """An `exhausted` package-installation failure the session carried on past;
    only surfaces if the turn then produced no content or did not end cleanly."""
    termination: TerminationReason | None = None
    """How the turn ended. Every driver exit sets it; `None` means still running
    (the reducer sets it early only when MA terminates the session)."""
    finished_tool_ids: tuple[str, ...] = ()
    """Ids of the tool calls that got a result, in the order the results came."""


def join_messages(texts: list[str]) -> str:
    """Join the messages of one answer; a lone message is returned as it is.

    With `turn.status_log` on, every message is kept and they are set apart by a
    blank line (blank messages add nothing). Off, texts are joined as they are,
    as before.
    """
    if len(texts) == 1:
        return texts[0]
    if not status_log_enabled():
        return "".join(texts)
    return "\n\n".join(stripped for text in texts if (stripped := text.strip()))


def _text_runs(content: list[ContentBlock]) -> list[tuple[int, list[TextBlock]]]:
    """Runs of consecutive text blocks as ``(index of the first block, blocks)``."""
    runs: list[tuple[int, list[TextBlock]]] = []
    previous_was_text = False
    for i, block in enumerate(content):
        if isinstance(block, TextBlock):
            if previous_was_text:
                runs[-1][1].append(block)
            else:
                runs.append((i, [block]))
        previous_was_text = isinstance(block, TextBlock)
    return runs


def _last_tool_index(content: list[ContentBlock]) -> int:
    return max(
        (i for i, block in enumerate(content) if isinstance(block, ToolUseBlock)), default=-1
    )


def final_answer_texts(content: list[ContentBlock]) -> list[str]:
    """The messages after the last tool use, one text each, in order."""
    return [
        block.text
        for block in content[_last_tool_index(content) + 1 :]
        if isinstance(block, TextBlock)
    ]


def extract_final_response(content: list[ContentBlock]) -> str:
    """Extract text after the last ToolUseBlock, or all text if no tools.

    In multi-tool turns, intermediate agent.message text (pre-tool narration)
    is positionally before ToolUseBlocks. Only text AFTER the last tool use
    is the actual response. When no tools were used, all text is final.
    With `turn.status_log` on, an answer of several messages keeps every one,
    set apart by a blank line.
    """
    return join_messages(final_answer_texts(content))


def extract_sealed_responses(
    content: list[ContentBlock], *, min_chars: int
) -> list[tuple[int, str]]:
    """Runs of text "sealed" by a later tool use and at least ``min_chars`` long.

    Text with a ToolUseBlock after it can never grow again, so it is safe to
    render permanently mid-turn. The length floor separates substantive
    answers from pre-tool narration ("Let me check…"). A run of consecutive
    messages is one candidate, its messages set apart by a blank line, so
    the floor sees what the old single glued block would have; with
    `turn.status_log` off every block is a candidate on its own.
    Returns ``(content index of the run's first block, text)`` pairs in stream
    order; the index is a stable dedup key for callers that flush
    incrementally. Text after the last tool use is the final response and is
    never included — that's ``extract_final_response``'s job.
    """
    last_tool_idx = _last_tool_index(content)
    sealed: list[tuple[int, str]] = []
    runs = (
        _text_runs(content)
        if status_log_enabled()
        else [(i, [block]) for i, block in enumerate(content) if isinstance(block, TextBlock)]
    )
    for index, blocks in runs:
        if index + len(blocks) > last_tool_idx:
            break
        # The floor measures the raw messages, as the old glued block did, so
        # whitespace the join trims can't push a run below it.
        if sum(len(block.text) for block in blocks) >= min_chars:
            sealed.append((index, join_messages([block.text for block in blocks])))
    return sealed
