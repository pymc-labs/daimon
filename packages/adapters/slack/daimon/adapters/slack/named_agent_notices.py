"""Block Kit refusal notices for explicit agent selection."""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.thread_handoff import HAND_OVER_ACTION_ID
from daimon.core.turn.errors import NamedAgentRefused


def build_named_agent_blocks(err: NamedAgentRefused) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": err.title}}
    ]
    if err.detail:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": escape_mrkdwn(err.detail)}}
        )
    if err.hand_over_agent_id is not None and err.hand_over_agent_name is not None:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": HAND_OVER_ACTION_ID,
                        "text": {
                            "type": "plain_text",
                            "text": f"Switch to {err.hand_over_agent_name}"[:75],
                        },
                        "value": err.hand_over_agent_id,
                        "style": "primary",
                    }
                ],
            }
        )
    return blocks
