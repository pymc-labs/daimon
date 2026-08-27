"""Pure turn-pipeline primitives for daimon-core.

No I/O lives in this package. The driver that consumes these primitives
(opening the SSE stream, calling lifecycle hooks on a timer, converting
`anthropic.APIError` to `TurnError(kind="upstream")`) lands in a later plan.
"""

from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent as SessionEvent
from daimon.core.turn.admission import Admission, AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.approvals import build_confirmation_events, pending_confirmation_ids
from daimon.core.turn.ceiling import (
    CEILING_MESSAGE,
    TURN_CEILING_S,
    ceiling_error,
    remaining_s,
    turn_deadline,
)
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.driver import run_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.posture import (
    AutoApprove,
    Billed,
    BillingExempt,
    BillingPosture,
    ExemptReason,
    RequireApproval,
    ToolConfirmation,
    UsageRecorder,
)
from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.core.turn.reducers import apply
from daimon.core.turn.render import (
    BlockAdded,
    BlockStatusChanged,
    RenderDelta,
    TextAppend,
    diff,
)
from daimon.core.turn.run import RunOutcome, run_prepared_turn
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
    "TurnDeps",
    # admission (D-01 stage one)
    "Admission",
    "AdmissionDenied",
    "MissingTurnConfigError",
    "admit",
    # billing posture
    "Billed",
    "BillingExempt",
    "BillingPosture",
    "ExemptReason",
    "UsageRecorder",
    # tool-confirmation posture
    "AutoApprove",
    "RequireApproval",
    "ToolConfirmation",
    "build_confirmation_events",
    "pending_confirmation_ids",
    # session preparation (D-01 stage two)
    "PreparedTurn",
    "bind_session",
    # per-turn ceiling
    "CEILING_MESSAGE",
    "TURN_CEILING_S",
    "ceiling_error",
    "remaining_s",
    "turn_deadline",
    # driver call + one-shot dead-session recovery
    "RunOutcome",
    "run_prepared_turn",
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
