"""Pure Block Kit state machine for Slack turn UX.

Ports the Discord ``embed.py`` state machine to Slack Block Kit, dropping the
color-based signaling.

Converts EmbedEvents and the turn's content into State and Block Kit dicts
with zero I/O dependencies. No ``slack_sdk`` or ``anthropic`` imports. The
in-progress card's words come from ``daimon.core.turn.status_lines``, shared
with Discord; ``escape_mrkdwn`` is imported from the sibling ``mrkdwn`` module
(same adapter package boundary; not a cross-adapter import).

Phase reference:
  THINKING     → *Working on it…*
  TOOL_RUNNING → *Working on it…*  (a tool call is running)
  DONE         → collapsed summary (terminal)
  ERROR        → ❌ collapsed summary (terminal)

Status surface shape (non-terminal):
  section  — *Working on it…*  (headline)
  section  — ```tool lines```  (when the turn has made tool calls)
  section  — > {escaped draft}  (when text_preview is set; expand=True)
  actions  — Cancel button (action_id="cancel_turn"; style="danger"; no value)

Terminal collapse (DONE/ERROR):
  section  — outcome and retry step when needed
  context  — Details: time, cost, tokens, balance

No color field anywhere — blocks only, no attachments.
Preview text entity-escaped via escape_mrkdwn (& first, then < >).
Terminal metrics stay visible in Details.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.turn.card_state import (
    CardEvent as _CardEvent,
)
from daimon.core.turn.card_state import (
    CardState as State,
)
from daimon.core.turn.card_state import (
    TurnPhase as TurnPhase,
)
from daimon.core.turn.card_state import (
    update as update,
)
from daimon.core.turn.card_state import (
    update_activity as update_activity,
)
from daimon.core.turn.notices import TerminationNotice, fit_notice
from daimon.core.turn.status_lines import (
    format_duration,
    format_headline,
)

EmbedEvent = _CardEvent

# Slack rejects a section block whose text exceeds 3,000 characters.
NOTICE_MAX_CHARS = 2900

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMOJI_CROSS = "❌"  # ❌

_TERMINAL_PHASES = frozenset({TurnPhase.DONE, TurnPhase.ERROR})

# Copy is byte-identical to the Discord adapter's orphan-retirement embed: the
# two adapters must say the same thing about the same event.
INTERRUPTED_NOTICE: str = "Stopped: Daimon restarted.\nMention me to try again."

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def _fmt_tokens(n: int) -> str:
    """Humanize a token count: <1000 verbatim, else one-decimal k with trailing
    ``.0`` stripped (``320`` -> ``"320"``, ``1500`` -> ``"1.5k"``, ``12000`` -> ``"12k"``)."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def to_fallback_text(state: State, *, now: float | None) -> str:
    """The running card's headline in plain words, e.g. ``Working on it…``.

    Slack shows it in notifications and to screen readers instead of the blocks.
    """
    return _headline(state, now, bold=lambda word: word)


def _headline(state: State, now: float | None, *, bold: Callable[[str], str]) -> str:
    elapsed_seconds = now - state.started_at if now is not None and state.started_at else None
    return format_headline(
        is_working=state.phase is TurnPhase.TOOL_RUNNING,
        elapsed_seconds=elapsed_seconds,
        bold=bold,
    )


def to_blocks(
    state: State,
    *,
    now: float | None,
    cancel_key: str | None = None,
    answer_visible: bool = False,
) -> list[dict[str, Any]]:
    """Render State into a list of Slack Block Kit block dicts.

    Args:
        state: Current turn State.
        now: Current monotonic time from the caller (``time.monotonic()``).
             Pass ``None`` to omit the elapsed line.
        cancel_key: Optional per-turn routing key for the live Cancel button.

    Returns:
        A list of raw block dicts safe to pass directly to Slack's ``blocks=``
        parameter. No ``slack_sdk.models.blocks`` types — pure dicts (Pitfall 5).

    Non-terminal (THINKING / TOOL_RUNNING):
        - section  : ``*Working on it…*``
        - section  : the tool lines in a code block  (when there are any)
        - section  : > {escaped draft}  (when text_preview is set; expand=True)
        - actions  : Cancel button  (action_id="cancel_turn", style="danger")

    Terminal (DONE / ERROR):
        - context  : Details with time, cost, tokens and balance
                     ERROR adds a separate notice section when available
        No actions block (cancel button removed on terminal).
    """
    if state.phase in _TERMINAL_PHASES:
        # Terminal collapse: outcome above, metrics grouped under Details.
        elapsed = int(now - state.started_at) if now is not None else 0
        tokens = f"{_fmt_tokens(state.usage_in)} in / {_fmt_tokens(state.usage_out)} out"
        parts: list[str] = [f"Time: {elapsed}s"]
        if state.cost_str is not None:
            parts.append(f"Cost: {state.cost_str}")
        parts.append(f"Tokens: {tokens}")
        if state.balance_str is not None:
            parts.append(f"Balance: {state.balance_str}")
        blocks: list[dict[str, Any]] = []
        if state.phase is TurnPhase.ERROR:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "Something went wrong."}}
            )
            next_step = "Mention me to try again."
            if state.notice and "*Next:* " in state.notice:
                next_step = state.notice.split("*Next:* ", 1)[1].split("\n", 1)[0]
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": next_step}})
            if state.notice:
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Details*\n{state.notice}"},
                    }
                )
        elif not answer_visible and state.text_preview is None:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "Done."}})
        if blocks:
            blocks.append({"type": "divider"})
        if state.agent_name and not state.header_customized:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": state.agent_name}]}
            )
        details_label = "" if state.phase is TurnPhase.ERROR and state.notice else "*Details*\n"
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": details_label + "\n".join(parts)}],
            }
        )
        return blocks

    # Non-terminal: the headline, the tool lines, then the latest draft.
    headline = _headline(state, now, bold=lambda word: f"*{word}*")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": headline}},
    ]
    if state.tool_lines:
        tool_lines = escape_mrkdwn("\n".join(state.tool_lines))
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"*Details*\n{tool_lines}"}],
            }
        )
    # expand=True keeps Slack from folding the draft behind "see more".
    if state.text_preview:
        blocks.append(
            {
                "type": "section",
                "expand": True,
                "text": {"type": "mrkdwn", "text": f"> {escape_mrkdwn(state.text_preview)}"},
            }
        )

    # Cancel button — present only while the turn is running (non-terminal).
    # A per-turn value lets the listener route clicks before Slack returns the
    # message ts from chat.postMessage.
    cancel_button: dict[str, Any] = {
        "type": "button",
        "action_id": "cancel_turn",
        "text": {"type": "plain_text", "text": "Stop"},
        "style": "danger",
    }
    if cancel_key is not None:
        cancel_button["value"] = cancel_key
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            "elements": [cancel_button],
        }
    )
    if now is not None and state.started_at:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": format_duration(now - state.started_at)}],
            }
        )

    return blocks


def to_interrupted_blocks() -> list[dict[str, Any]]:
    """Render the frozen status card a boot sweep leaves behind.

    Deliberately NOT ``to_blocks(State(phase=TurnPhase.ERROR, ...))``: a fresh
    boot process has the DB row and nothing else -- no agent name, no usage,
    no monotonic start -- so the terminal collapse would render an empty
        agent field and misleading zero usage for a turn that may have
    run 40 minutes.

    Takes no arguments and emits no ``actions`` block, so the Cancel button is
    gone by construction -- there is no live turn left to cancel.
    """
    title, next_step = INTERRUPTED_NOTICE.split("\n", 1)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": next_step}},
    ]


def format_termination_notice(notice: TerminationNotice) -> str:
    """Draw the core notice below the ERROR card's title as mrkdwn."""
    lines = [escape_mrkdwn(notice.cause)]
    work = notice.work_line(lambda name: f"`{escape_mrkdwn(name.replace('`', ''))}`")
    if work is not None:
        lines.append(work)
    lines.append(notice.survived)
    lines.append(f"*Next:* {notice.next_step}")
    tail = f"`rid: {notice.request_id}`" if notice.request_id is not None else None
    return fit_notice(lines, tail=tail, limit=NOTICE_MAX_CHARS)
