"""Post the GitHub App install-page link as a Slack link button.

Posted from the MCP process. Like its Discord sibling
(`tools/discord/_app_install_button.py`) and unlike the credential button,
this button carries a ``url`` and no ``action_id`` value the bot process
dispatches — Slack opens the URL itself when it is clicked. Nothing here
creates a request row, a minted token, an expiry, or a handler, and nothing
should.

Reuses the send path's target and visibility discipline: the composite
``channel_id:thread_ts`` form lands the button in the thread the request was
made from, and the requester must be able to see the channel before
anything is posted.

The install URL comes from `daimon.core.github_app_auth` — the single place
it is constructed.
"""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._send import (
    _split_send_target,  # pyright: ignore[reportPrivateUsage]
    _validate_channel_access,  # pyright: ignore[reportPrivateUsage]
    _validate_thread_target,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._visibility import map_slack_api_error
from daimon.core.github_app_auth import build_app_install_url
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError

# Slack renders a button whose action_id it has never seen as a no-op click
# with a warning; a url button still needs one to be a valid element.
_ACTION_ID = "daimon_github_app_install_link"


def _build_message_text(*, requester_platform_user_id: str, purpose: str) -> str:
    """The message body, in Slack mrkdwn (single-asterisk bold, `<@U…>` mention)."""
    return (
        f"<@{requester_platform_user_id}> wants to reach a private GitHub repository "
        f"({purpose}) — installing the GitHub App grants read access to the "
        "repositories you choose, so skill sync and repo cloning can reach them "
        "without anyone pasting a token.\n"
        "Click the button below to open the install page on GitHub."
    )


async def _post_slack_app_install_button_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    slug: str,
    purpose: str,
) -> str:
    """Post the App install link as a Slack link button. Returns the sent message ts."""
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    target_channel_id, thread_ts = _split_send_target(channel_id)
    client = await slack_web_client(runtime, team_id=team_id)

    await _validate_channel_access(client, channel_id=target_channel_id, requester_id=requester_id)
    if thread_ts is not None:
        await _validate_thread_target(client, channel_id=target_channel_id, thread_ts=thread_ts)

    text = _build_message_text(requester_platform_user_id=requester_id, purpose=purpose)
    post_kwargs: dict[str, Any] = {
        "channel": target_channel_id,
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": _ACTION_ID,
                        "url": build_app_install_url(slug),
                        "text": {"type": "plain_text", "text": "Install the GitHub App"},
                    }
                ],
            },
        ],
    }
    if thread_ts is not None:
        post_kwargs["thread_ts"] = thread_ts
    try:
        sent = await client.chat_postMessage(**post_kwargs)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]  # slack_sdk **kwargs: Unknown
    except SlackApiError as err:
        mapped = map_slack_api_error(err)
        if mapped is not None:
            raise mapped from err
        code = str(err.response.get("error", "slack_api_error"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        raise ToolError(f"posting to the channel failed ({code})") from err
    return str(cast(dict[str, Any], sent.data)["ts"])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # slack_sdk response is dict-like
