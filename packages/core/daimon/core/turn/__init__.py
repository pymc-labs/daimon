"""Pure turn-pipeline primitives for daimon-core.

No I/O lives in this package. The driver that consumes these primitives
(opening the SSE stream, calling lifecycle hooks on a timer, converting
`anthropic.APIError` to `TurnError(kind="upstream")`) lands in a later plan.
"""

from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent as SessionEvent
from daimon.core.turn.driver import run_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.reducers import apply
from daimon.core.turn.render import (
    BlockAdded,
    BlockStatusChanged,
    RenderDelta,
    TextAppend,
    diff,
)
from daimon.core.turn.state import (
    ContentBlock,
    Task,
    TextBlock,
    ToolUseBlock,
    TurnState,
)

__all__ = [
    # SDK alias (re-exported for short imports in driver/test code)
    "SessionEvent",
    "TurnLifecycle",
    # state
    "ContentBlock",
    "Task",
    "TextBlock",
    "ToolUseBlock",
    "TurnState",
    # reducers
    "apply",
    # driver
    "run_turn",
    # render
    "BlockAdded",
    "BlockStatusChanged",
    "RenderDelta",
    "TextAppend",
    "diff",
]
