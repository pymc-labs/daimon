"""The support channel post of a limit request: its lines, settle buttons and closing.

Shared by Ask a person (`support_escalation`) and its settle controls
(`support_settle`). The buttons carry the request id, so they work after a restart.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from daimon.adapters.slack.split import split_for_slack_safe
from daimon.core.slack_mrkdwn import escape_mrkdwn
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support import (
    ACCEPT,
    CLIENT_NOT_NOTIFIED,
    DECLINE,
    SET_CHARGE,
    option_text,
    post_status_text,
)

SETTLE_SET_ACTION_ID: Final = "support_settle"
SETTLE_ACCEPT_ACTION_ID: Final = "support_accept"
SETTLE_DECLINE_ACTION_ID: Final = "support_decline"
NOT_NOTIFIED_LINE: Final = f"_{CLIENT_NOT_NOTIFIED}_"
_FACTS_BLOCK_ID: Final = "support_status"
_NOT_NOTIFIED_BLOCK_ID: Final = "support_not_notified"
_SETTLE_BLOCK_ID: Final = "support_settle_actions"
_SECTION_CHARS: Final = 3000


@dataclass(frozen=True)
class SupportPost:
    """Where a post landed; `ts` is the message id the settle edits."""

    channel_id: str
    ts: str | None


def status_line(row: SupportEscalationRow) -> str:
    return post_status_text(
        status=row.status, charged_to=row.charged_to, charge=row.charge, bounds=row.bounds
    )


def request_facts(row: SupportEscalationRow, *, menu: bool, balance: str | None) -> str:
    """A limit request's lines: its terms on a configured menu, status and balance."""
    lines = [escape_mrkdwn(option_text(row.option))] if menu and row.option is not None else []
    lines.append(status_line(row))
    if balance:
        lines.append(balance)
    return "\n".join(lines)


def _section(text: str, block_id: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if block_id is not None:
        block["block_id"] = block_id
    return block


def _button(action_id: str, label: str, value: str, *, primary: bool) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": label},
        "value": value,
    }
    if primary:
        button["style"] = "primary"
    return button


def pending_post_blocks(
    *,
    head: str,
    facts: str,
    rest: Sequence[str],
    note: str,
    escalation_id: uuid.UUID,
    accept: bool = False,
) -> list[dict[str, Any]]:
    """The post as blocks: facts, the decision-8 warning, Accept or Set charge, and Decline."""
    value = escalation_id.hex
    first = (
        _button(SETTLE_ACCEPT_ACTION_ID, ACCEPT, value, primary=True)
        if accept
        else _button(SETTLE_SET_ACTION_ID, SET_CHARGE, value, primary=True)
    )
    notes = [_section(chunk) for chunk in split_for_slack_safe(note, _SECTION_CHARS) if chunk]
    return [
        _section(head),
        _section(facts, _FACTS_BLOCK_ID),
        {
            "type": "context",
            "block_id": _NOT_NOTIFIED_BLOCK_ID,
            "elements": [{"type": "mrkdwn", "text": NOT_NOTIFIED_LINE}],
        },
        *(_section(part) for part in rest),
        *notes,
        {
            "type": "actions",
            "block_id": _SETTLE_BLOCK_ID,
            "elements": [
                first,
                _button(SETTLE_DECLINE_ACTION_ID, DECLINE, value, primary=False),
            ],
        },
    ]


def _settled_lines(text: str, *, pending_line: str, final_line: str) -> str:
    lines = [final_line if line == pending_line else line for line in text.split("\n")]
    return "\n".join(line for line in lines if line != NOT_NOTIFIED_LINE)


def closed_post(
    message: dict[str, Any], *, pending_line: str, final_line: str
) -> tuple[str, list[dict[str, Any]]]:
    """The post once settled: the final status in place of the pending one, no buttons."""
    text = _settled_lines(
        str(message.get("text") or ""), pending_line=pending_line, final_line=final_line
    )
    if final_line not in text.split("\n"):
        text = f"{text}\n\n{final_line}" if text else final_line
    blocks: list[dict[str, Any]] = []
    placed = False
    for raw in cast("list[Any]", message.get("blocks") or []):
        if not isinstance(raw, dict):
            continue
        block = cast("dict[str, Any]", raw)
        block_id = block.get("block_id")
        if block_id in (_NOT_NOTIFIED_BLOCK_ID, _SETTLE_BLOCK_ID):
            continue
        if block_id == _FACTS_BLOCK_ID:
            facts = str(cast("dict[str, Any]", block.get("text") or {}).get("text") or "")
            facts = _settled_lines(facts, pending_line=pending_line, final_line=final_line)
            if final_line not in facts.split("\n"):
                facts = f"{facts}\n{final_line}" if facts else final_line
            block, placed = _section(facts, _FACTS_BLOCK_ID), True
        blocks.append(block)
    if not placed:
        blocks.append(_section(final_line, _FACTS_BLOCK_ID))
    return text, blocks


__all__ = [
    "NOT_NOTIFIED_LINE",
    "SETTLE_ACCEPT_ACTION_ID",
    "SETTLE_DECLINE_ACTION_ID",
    "SETTLE_SET_ACTION_ID",
    "SupportPost",
    "closed_post",
    "pending_post_blocks",
    "request_facts",
    "status_line",
]
