"""Pure snapshot-diff render helper.

`diff(prev, curr)` returns a `RenderDelta` describing what changed between
two `TurnState` snapshots. The render loop in the driver calls this on a
fixed cadence and passes the delta (or just the current state) to the
adapter-supplied `TurnLifecycle.on_render`; adapters use the delta to emit
only what's new.

Invariants (follow from the reducer's append-only discipline):

- `curr.content` is `prev.content` with possibly-extended `TextBlock.text`,
  possibly-updated `ToolUseBlock.status`/`result`, and zero or more new
  blocks at the end. No block is ever removed or reordered.
- Scalar fields (`stop_reason`, `error`, `rate_limit_until`) only transition
  from `None` to a terminal value; the diff only reports the transition.

Consequently the diff does not need to model deletions, reorderings, or
text truncations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    StopReason,
)
from daimon.core.errors import TurnError
from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock, TurnState


@dataclass(frozen=True, slots=True)
class TextAppend:
    """Existing `TextBlock` at `block_index` grew by `appended_text`."""

    block_index: int
    appended_text: str


@dataclass(frozen=True, slots=True)
class BlockAdded:
    """A block appeared at `block_index`. For `TextBlock`, the full text is
    carried; for `ToolUseBlock`, this is the initial pending block."""

    block_index: int
    block: ContentBlock


@dataclass(frozen=True, slots=True)
class BlockStatusChanged:
    """A `ToolUseBlock` transitioned pending → complete/failed or otherwise
    changed its status/result. Full updated block is carried so renderers do
    not need to cross-reference `curr`."""

    block_index: int
    block: ToolUseBlock


@dataclass(frozen=True, slots=True)
class RenderDelta:
    """Structured description of what changed between two `TurnState`
    snapshots. Pure value object — renderers interpret it.
    """

    text_appends: list[TextAppend] = field(default_factory=list[TextAppend])
    block_additions: list[BlockAdded] = field(default_factory=list[BlockAdded])
    block_status_changes: list[BlockStatusChanged] = field(default_factory=list[BlockStatusChanged])
    stop_reason_set: StopReason | None = None
    error_set: TurnError | None = None
    rate_limit_set: datetime | None = None

    def is_empty(self) -> bool:
        return (
            not self.text_appends
            and not self.block_additions
            and not self.block_status_changes
            and self.stop_reason_set is None
            and self.error_set is None
            and self.rate_limit_set is None
        )


def diff(prev: TurnState | None, curr: TurnState) -> RenderDelta:
    """Produce a `RenderDelta` describing what's new in `curr` vs `prev`.

    `prev=None` is handled as "first render — everything is new," which
    yields a `BlockAdded` per block in `curr`. Scalar diffs only fire on
    None→Some transitions.
    """
    if prev is None:
        return RenderDelta(
            block_additions=[
                BlockAdded(block_index=i, block=block) for i, block in enumerate(curr.content)
            ],
            stop_reason_set=curr.stop_reason,
            error_set=curr.error,
            rate_limit_set=curr.rate_limit_until,
        )

    text_appends: list[TextAppend] = []
    status_changes: list[BlockStatusChanged] = []
    additions: list[BlockAdded] = []

    shared_count = min(len(prev.content), len(curr.content))
    for i in range(shared_count):
        prev_block = prev.content[i]
        curr_block = curr.content[i]
        if isinstance(prev_block, TextBlock) and isinstance(curr_block, TextBlock):
            if curr_block.text != prev_block.text:
                # Reducer only appends; curr.text must start with prev.text.
                suffix = curr_block.text[len(prev_block.text) :]
                text_appends.append(TextAppend(block_index=i, appended_text=suffix))
        elif (
            isinstance(prev_block, ToolUseBlock)
            and isinstance(curr_block, ToolUseBlock)
            and (
                prev_block.status != curr_block.status
                or prev_block.result_content != curr_block.result_content
                or prev_block.is_error != curr_block.is_error
            )
        ):
            status_changes.append(BlockStatusChanged(block_index=i, block=curr_block))
        # Mixed types at the same index would be a reducer bug — impossible
        # under the append-only invariant. Don't defensively handle it.

    for i in range(shared_count, len(curr.content)):
        additions.append(BlockAdded(block_index=i, block=curr.content[i]))

    return RenderDelta(
        text_appends=text_appends,
        block_additions=additions,
        block_status_changes=status_changes,
        stop_reason_set=(curr.stop_reason if curr.stop_reason != prev.stop_reason else None),
        error_set=(curr.error if curr.error is not prev.error else None),
        rate_limit_set=(
            curr.rate_limit_until if curr.rate_limit_until != prev.rate_limit_until else None
        ),
    )
