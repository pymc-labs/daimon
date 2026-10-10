"""Slack /memory handler — read-only view of the channel agent's memory.

Ack-first contract: called from app.on_request AFTER the Socket Mode ack.
Ephemeral-only (no modal, no trigger_id), mirroring help.py. The slash
payload's `text` is the optional memory path argument.

Catches DaimonError | anthropic.APIError | SlackApiError at the listener
boundary (S3).
"""

from __future__ import annotations

import contextlib
from typing import Any

import anthropic
import structlog
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.memory_view import (
    get_channel_memory_store,
    get_memory_content,
    list_memory_paths,
)
from slack_sdk.errors import SlackApiError

log = structlog.get_logger()

_SLACK_LIMIT = 3800  # headroom under Slack's ~4000-char text limit
_EMPTY = "No memory to show here."


def _truncate(text: str) -> str:
    if len(text) <= _SLACK_LIMIT:
        return text
    return text[: _SLACK_LIMIT - 20] + "\n… (truncated)"


def _fenced(header: str, content: str, limit: int) -> str:
    """Wrap content in a closed code fence, truncating content to fit limit.

    Truncating the CONTENT before wrapping (rather than truncating the fully
    wrapped string) guarantees the closing ``` fence is always present — a
    naive `_truncate(header + fence + content + fence)` can slice mid-fence
    and leave an unclosed code block that corrupts rendering. Backtick runs
    inside the content get a zero-width space so an embedded ``` can't close
    the wrapping fence early.
    """
    content = content.replace("```", "`​``")
    overhead = len(header) + len("\n```\n\n```")
    budget = limit - overhead
    if len(content) > budget:
        content = content[: budget - 16] + "\n… (truncated)"
    return f"{header}\n```\n{content}\n```"


async def handle_memory_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Ephemeral /memory handler.

    Ack-first contract: called from app.on_request AFTER the Socket Mode ack.
    Resolves the per-event web client, then posts a chat.postEphemeral listing
    memory paths (no argument) or showing one memory's content (path argument).

    Catches DaimonError | anthropic.APIError | SlackApiError at the listener
    boundary (S3).
    """
    team_id: str = str(payload.get("team_id") or "")
    user_id: str = str(payload.get("user_id") or "")
    channel_id: str = str(payload.get("channel_id") or "")
    path_arg: str = str(payload.get("text") or "").strip()

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.memory_command.no_token", team_id=team_id)
        return

    try:
        resolved = await get_channel_memory_store(
            runtime.sessionmaker,
            runtime.anthropic,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
            platform="slack",
            user_id=user_id,
            channel_id=channel_id,
            default=runtime.deployment_default,
        )
        if resolved is None:
            text = _EMPTY
        elif not path_arg:
            agent_name, store_id = resolved
            paths = await list_memory_paths(runtime.anthropic, store_id)
            text = (
                _EMPTY
                if not paths
                else _truncate(
                    f"*{agent_name}'s memory* ({len(paths)} files)\n"
                    + "\n".join(f"• `{p}`" for p in paths)
                )
            )
        else:
            _agent_name, store_id = resolved
            content = await get_memory_content(runtime.anthropic, store_id, path_arg)
            if content is None:
                text = f"No memory file called `{path_arg}`.\n\nRun `/memory` to see them all."
            else:
                text = _fenced(f"*`{path_arg}`*", content, _SLACK_LIMIT)

        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id,
            user=user_id,
            text=text,
        )
    except (DaimonError, anthropic.APIError, SlackApiError) as exc:
        log.warning("slack.memory_command.failed", team_id=team_id, exc_info=exc)
        error_text = (
            str(exc)
            if isinstance(exc, DaimonError)
            else "Couldn't load the memory.\n\nTry again in a minute."
        )
        with contextlib.suppress(SlackApiError):
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=error_text,
            )
