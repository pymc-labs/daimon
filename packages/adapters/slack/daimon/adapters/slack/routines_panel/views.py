"""Pure Block Kit view builders for the /routines panel (SUX-02, S4).

All functions return plain ``dict[str, Any]`` objects — no ``slack_sdk.models``
types (project convention per Pitfall 5 / S4). This module imports only stdlib
and ``daimon.adapters.slack.mrkdwn`` (no slack_sdk, no daimon.core stores).

Pitfall 5: all user/agent-derived text is run through ``escape_mrkdwn``.
Pitfall 6: modal title ≤ 24 chars; overflow options ≤ 25.
"""

from __future__ import annotations

import json
from typing import Any

from daimon.adapters.slack.agent_setup.state import encode_private_metadata
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.routines_panel.state import RoutineEntry, RoutinesPanelState

__all__ = [
    "build_content_view",
    "build_create_routine_modal",
    "build_delete_confirm_modal",
    "build_last_output_view",
    "build_loading_view",
]

_MAX_OUTPUT_CHARS = 1000
_EMPTY_HINT = (
    "_No routines yet._ Ask your agent to schedule one, e.g. _'daily 9am stand-up summary'_."
)


def build_loading_view(*, channel_id: str = "") -> dict[str, Any]:
    """Return a lightweight Loading… modal to open immediately on /routines.

    Opened with the fresh ``trigger_id`` (D-06) so the ~3s window is never
    blown. The channel_id is stored in ``private_metadata`` so overflow action
    handlers can send ephemeral messages back to the original channel.

    Args:
        channel_id: Original channel from the slash-command payload.

    Returns:
        A modal view dict safe to pass to ``views_open(view=...)``.
    """
    return {
        "type": "modal",
        "callback_id": "routines_panel",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Routines"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Loading…"},
            }
        ],
    }


def _routine_row_block(entry: RoutineEntry) -> dict[str, Any]:
    """Build one section block with an overflow (⋯) accessory for a single entry.

    The accessory ``action_id`` is ``routine_action:{routine_id}`` so the
    dispatcher can parse the routine UUID directly from the action_id string.
    Options: pause / resume / "View last output" (output).
    """
    text = f"{entry.glyph} *{escape_mrkdwn(entry.label)}* · {escape_mrkdwn(entry.agent_name)}"
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
        "accessory": {
            "type": "overflow",
            "action_id": f"routine_action:{entry.routine.id}",
            "options": [
                {"text": {"type": "plain_text", "text": "Pause"}, "value": "pause"},
                {"text": {"type": "plain_text", "text": "Resume"}, "value": "resume"},
                {
                    "text": {"type": "plain_text", "text": "View last output"},
                    "value": "output",
                },
                {"text": {"type": "plain_text", "text": "Delete"}, "value": "delete"},
            ],
        },
    }


def build_content_view(state: RoutinesPanelState, *, channel_id: str = "") -> dict[str, Any]:
    """Return the populated routines panel modal with one section per entry.

    Layout:
    - One ``section`` block per RoutineEntry with an ``overflow`` accessory.
    - An optional ``context`` note when over_cap_count > 0.
    - A trailing ``actions`` block with a Refresh button.

    Args:
        state:      Panel state carrying the capped entry list.
        channel_id: Threaded from the original slash command for ephemeral replies.

    Returns:
        A modal view dict safe to pass to ``views_update(view=...)``.
    """
    blocks: list[dict[str, Any]] = []

    if not state.rows:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _EMPTY_HINT}})
    else:
        for entry in state.rows:
            blocks.append(_routine_row_block(entry))

    if state.over_cap_count > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"_+{state.over_cap_count} more routine(s) not shown "
                            f"(cap: {_PICKER_CAP})_"
                        ),
                    }
                ],
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "routines_refresh",
                    "text": {"type": "plain_text", "text": "↻ Refresh"},
                },
                {
                    "type": "button",
                    "action_id": "routines_create",
                    "text": {"type": "plain_text", "text": "+ New Routine"},
                },
            ],
        }
    )

    return {
        "type": "modal",
        "callback_id": "routines_panel",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Routines"},
        "blocks": blocks,
    }


def build_last_output_view(text: str) -> str:
    """Return the last-output body as a fenced code block string for ephemeral display.

    Caps at 1000 chars with a truncation note (port of discord subviews.py).
    The caller passes this string to ``chat.postEphemeral(text=...)``.

    Args:
        text: ``last_error`` or ``last_result_tail`` from the routine row.

    Returns:
        A mrkdwn-safe fenced block string (no escape needed — code blocks are literal).
    """
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + "\n… (truncated)"
    return f"```\n{text}\n```"


def build_create_routine_modal(
    *, team_id: str, channel_id: str, agent_names: list[str]
) -> dict[str, Any]:
    """Return the New Routine creation modal (callback_id ``routines__create``).

    The routine is created directly from this Slack interaction — which carries
    the real user id — rather than via the MCP ``create_routine`` tool (whose
    agent token never carries a ``platform_user_id``).

    Carries ``team_id`` + ``channel_id`` in ``private_metadata`` (via
    ``encode_private_metadata``) so the post-ack run can re-derive the tenant and
    post confirmation/refusal ephemerals back to the invoking channel.

    Args:
        team_id:     Workspace ID, threaded into private_metadata.
        channel_id:  Invoking channel, threaded into private_metadata.
        agent_names: Tenant agent display names for the agent dropdown options.

    Returns:
        A modal view dict safe to pass to ``views_push(view=...)``.
    """
    agent_options: list[dict[str, Any]] = [
        {"text": {"type": "plain_text", "text": name[:75]}, "value": name} for name in agent_names
    ]
    agent_element: dict[str, Any] = {
        "type": "static_select",
        "action_id": "routines_create__agent",
        "placeholder": {"type": "plain_text", "text": "Pick an agent"},
    }
    if agent_options:
        agent_element["options"] = agent_options

    return {
        "type": "modal",
        "callback_id": "routines__create",
        "private_metadata": encode_private_metadata(team_id=team_id, channel_id=channel_id),
        "title": {"type": "plain_text", "text": "New Routine"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "routines_create__agent",
                "label": {"type": "plain_text", "text": "Agent"},
                "element": agent_element,
            },
            {
                "type": "input",
                "block_id": "routines_create__cron",
                "label": {"type": "plain_text", "text": "Cron expression"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "routines_create__cron",
                    "placeholder": {"type": "plain_text", "text": "0 18 * * *"},
                },
            },
            {
                "type": "input",
                "block_id": "routines_create__timezone",
                "label": {"type": "plain_text", "text": "Timezone"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "routines_create__timezone",
                    "initial_value": "UTC",
                },
            },
            {
                "type": "input",
                "block_id": "routines_create__message",
                "label": {"type": "plain_text", "text": "Trigger message"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "routines_create__message",
                    "multiline": True,
                },
            },
        ],
    }


def build_delete_confirm_modal(
    *,
    team_id: str,
    channel_id: str,
    routine_id: str,
    root_view_id: str,
    label: str,
) -> dict[str, Any]:
    """Return the Delete-confirmation modal (callback_id ``routines__delete_confirm``).

    Pushed on top of the panel when the user picks Delete from a routine's
    overflow menu. Delete is destructive and one-click, so it is gated behind
    this explicit confirm step.

    Carries ``team_id`` / ``channel_id`` / ``routine_id`` / ``root_view_id`` in
    ``private_metadata`` so the post-ack run can re-derive the tenant, delete the
    routine, refresh the underlying panel view (``root_view_id``) in place, and
    post the confirmation ephemeral back to the invoking channel.

    Args:
        team_id:      Workspace ID, threaded into private_metadata.
        channel_id:   Invoking channel, threaded into private_metadata.
        routine_id:   UUID string of the routine to delete.
        root_view_id: View id of the underlying panel, so it can be refreshed.
        label:        Human label for the routine (escaped for display).

    Returns:
        A modal view dict safe to pass to ``views_push(view=...)``.
    """
    metadata = json.dumps(
        {
            "team_id": team_id,
            "channel_id": channel_id,
            "routine_id": routine_id,
            "root_view_id": root_view_id,
        },
        separators=(",", ":"),
    )
    return {
        "type": "modal",
        "callback_id": "routines__delete_confirm",
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Delete routine"},
        "submit": {"type": "plain_text", "text": "Delete"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Delete *{escape_mrkdwn(label)}*?\n\nThis can't be undone.",
                },
            }
        ],
    }


# Module-level constant for context note so import-linter grep can find it
_PICKER_CAP = 25
