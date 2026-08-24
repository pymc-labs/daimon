"""Post the credential-request button to a Slack channel.

Posted from the MCP process (Cloud Run); dispatched later by the Slack bot
process via `handle_credential_request_click`. The two processes cannot
import each other (import-linter's independence contract), so this module
imports only the core wire-contract pieces (`SLACK_ACTION_ID`,
`build_button_label`) — never the Slack adapter's handler, and never a
re-derived action id. A divergent copy here would silently stop the button
from dispatching in the other process.

Unlike Discord's custom_id encoding, Slack routes block_actions by
`action_id` and a button carries a free-form `value`, so the opaque
single-use token rides in `value` under the fixed `SLACK_ACTION_ID`.

Reuses the read tools' channel-visibility discipline (`conversations.info` →
`check_channel_access`) so posting a credential button proves the requester
may see the channel before anything lands in it.

The button posts at the channel's top level, never in a thread. On Discord a
thread IS a channel, so `channel_id` alone lands the button where the
conversation is; a Slack thread is a (channel, ts) pair and the four request
tools' shared contract carries no `thread_ts`. Threading the button means
widening that cross-platform tool schema — until then, a request made
mid-thread posts its button to the channel root.
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
from daimon.core.credential_requests import (
    MAX_SLACK_BUTTON_LABEL_CHARS,
    SLACK_ACTION_ID,
    CredentialRequestKind,
    build_button_label,
)
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError

_KIND_NOUN: dict[CredentialRequestKind, str] = {
    "env": "an environment secret",
    "mcp": "an MCP server credential",
    "repo": "a repo binding",
    "skill_repo": "a GitHub token to import skills",
}


def _build_message_text(
    *,
    requester_platform_user_id: str,
    agent_name: str,
    kind: CredentialRequestKind,
    target: str,
    purpose: str,
) -> str:
    """The message body, in Slack mrkdwn (single-asterisk bold, `<@U…>` mention)."""
    if kind == "repo":
        return (
            f"<@{requester_platform_user_id}> wants to bind a repo to *{agent_name}* "
            f"(`{target}`) — {purpose}\n"
            "Click the button below to open a private form confirming the branch, "
            "and — only if the repo isn't publicly readable — a GitHub token that "
            "never appears in this channel."
        )
    return (
        f"<@{requester_platform_user_id}> *{agent_name}* needs {_KIND_NOUN[kind]} "
        f"(`{target}`) — {purpose}\n"
        "Click the button below to enter the value privately in a form; "
        "it will never appear in this channel.\n"
        f"Once added, this credential becomes usable by everyone who talks to "
        f"*{agent_name}*."
    )


async def _post_slack_credential_button_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    kind: CredentialRequestKind,
    target: str,
    token: str,
    agent_name: str,
    purpose: str,
) -> str:
    """Post a target-naming credential button. Returns the sent message ts."""
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    client = await slack_web_client(runtime, team_id=team_id)

    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        channel: dict[str, Any] = dict(info.get("channel") or {})  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        await check_channel_access(client, channel=channel, user_id=requester_id)
        text = _build_message_text(
            requester_platform_user_id=requester_id,
            agent_name=agent_name,
            kind=kind,
            target=target,
            purpose=purpose,
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
                            "action_id": SLACK_ACTION_ID,
                            "value": token,
                            "text": {
                                "type": "plain_text",
                                "text": build_button_label(
                                    kind, target, max_chars=MAX_SLACK_BUTTON_LABEL_CHARS
                                ),
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
