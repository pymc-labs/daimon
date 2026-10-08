"""The Hand over button on a Slack thread whose channel now answers with another agent.

Slack's side of `daimon.adapters.discord.thread_handoff`: the responder-changed
notice carries a button whose value is the agent the channel answers with now.
A click hands the thread to that agent for the person who clicked, decided by
`daimon.core.thread_handoff` under the tenant policy lock with their live
admin status (`users.info`) and channel admin grants, user groups confirmed
live. The value is only a request; an external Slack Connect member's click is
dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.channel_admin_groups import channel_admin_caller
from daimon.adapters.slack.gating import is_external_interactive
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.thread_handoff import switch_thread_on_request
from slack_sdk.errors import SlackApiError

__all__ = ["HAND_OVER_ACTION_ID", "build_hand_over_blocks", "handle_hand_over_click"]

log = structlog.get_logger()

# Disjoint from every other action_id app.py routes.
HAND_OVER_ACTION_ID: Final = "thread_hand_over"


def build_hand_over_blocks(*, text: str, agent_id: str, agent_name: str) -> list[dict[str, Any]]:
    """Separate the thread state, supported action, and new-thread hint."""
    summary, _, hint = text.partition("\n")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": HAND_OVER_ACTION_ID,
                    "text": {"type": "plain_text", "text": f"Switch to {agent_name}"[:75]},
                    "value": agent_id,
                    "style": "primary",
                }
            ],
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": hint}]},
    ]


async def handle_hand_over_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Switch the clicked thread to the button's agent, or say privately why not."""
    if is_external_interactive(payload):
        log.info("thread_handoff.external_click_rejected")
        return
    team: dict[str, Any] = payload.get("team") or {}
    user: dict[str, Any] = payload.get("user") or {}
    channel: dict[str, Any] = payload.get("channel") or {}
    container: dict[str, Any] = payload.get("container") or {}
    message: dict[str, Any] = payload.get("message") or {}
    team_id = str(team.get("id") or "")
    user_id = str(user.get("id") or "")
    channel_id = str(channel.get("id") or container.get("channel_id") or "")
    notice_ts = str(container.get("message_ts") or message.get("ts") or "")
    thread_ts = str(message.get("thread_ts") or container.get("thread_ts") or "")
    actions: list[dict[str, Any]] = payload.get("actions") or []
    agent_id = str(actions[0].get("value") or "") if actions else ""
    if not (team_id and user_id and channel_id and thread_ts and agent_id):
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    outcome = await switch_thread_on_request(
        runtime.anthropic,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id=channel_id,
        thread_id=thread_ts,
        ma_agent_id=agent_id,
        caller=await channel_admin_caller(
            runtime,
            client,
            tenant_id=tenant_id,
            user_id=user_id,
            is_admin=await resolve_is_admin(client, user_id=user_id),
        ),
        default=runtime.deployment_default,
        channel=f"<#{channel_id}>",
        now=datetime.now(UTC),
    )
    log.info(
        "thread_handoff.clicked", platform="slack", switched=outcome.switched, agent_id=agent_id
    )
    try:
        if not outcome.switched:
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id, user=user_id, thread_ts=thread_ts, text=outcome.text
            )
            return
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"<@{user_id}> handed this conversation over. {outcome.text}",
        )
        if notice_ts:
            # The notice's button has done its job; keep its text.
            notice_text = str(message.get("text") or "")
            await client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id, ts=notice_ts, text=notice_text, blocks=[]
            )
    except SlackApiError as err:
        log.warning(
            "thread_handoff.notice_failed",
            error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        )
