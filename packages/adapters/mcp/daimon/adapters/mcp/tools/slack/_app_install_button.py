"""Post a GitHub App install-page link through the caller's Slack workspace.

A Slack URL button still sends an interaction payload. The Slack adapter
acknowledges every block-action envelope before dispatch, and its action-id
ladder silently ignores unknown ids; this inert link needs no handler.
No credential request, token or expiry is created. Channel access is checked
before posting. With no thread timestamp in the tool schema, this posts at
the channel root. The install URL comes only from configured App identity.
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.core.github_app_auth import build_app_install_url
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError


async def _post_slack_app_install_button_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    slug: str,
    purpose: str,
) -> str:
    """Post the configured GitHub App install link. Returns the sent message ts."""
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    client = await slack_web_client(runtime, team_id=team_id)

    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        # Slack responses and the shared visibility API expose an open channel mapping.
        channel: dict[str, Any] = dict(info.get("channel") or {})  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        await check_channel_access(client, channel=channel, user_id=requester_id)
        text = (
            f"<@{requester_id}> — {purpose}\n"
            "Install the GitHub App to choose repositories it may read. "
            "Installing alone does not verify this workspace's access or bind a working repo. "
            "A working GitHub token remains an alternative; existing bound tokens stay in use."
        )
        sent = await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "github_app_install_link_inert",
                            "url": build_app_install_url(slug),
                            "text": {
                                "type": "plain_text",
                                "text": "Install GitHub App",
                            },
                        }
                    ],
                },
            ],
        )
    except SlackApiError as err:
        code = str(err.response.get("error", "slack_api_error"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        raise ToolError(f"posting to the channel failed ({code})") from err
    return str(sent.get("ts") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
