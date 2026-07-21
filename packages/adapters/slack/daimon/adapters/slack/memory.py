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
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import ScopeContext
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from slack_sdk.errors import SlackApiError

log = structlog.get_logger()

_SLACK_LIMIT = 3800  # headroom under Slack's ~4000-char text limit
_EMPTY = "This agent has no memories yet — it will start remembering as it works."


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


async def _resolve_store(
    runtime: SlackRuntime, *, team_id: str, user_id: str, channel_id: str
) -> tuple[str, str] | None:
    """Resolve (agent_name, memory_store_id) for the invoking channel.

    Returns None when the channel has no configured agent or the agent has no
    memory store yet. Raises DaimonError when the configured agent doesn't
    exist on the MA side. Same resolution chain as the Discord /memory command.
    """
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        scope = ScopeContext(
            account_id=principal.account_id,
            tenant_id=tenant_id,
            channel_id=channel_id,
        )
        config = await resolve_config(session, context=scope, default=runtime.deployment_default)
    if config.agent_name is None:
        return None
    agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=config.agent_name
    )
    if agent is None:
        raise DaimonError(f"Configured agent *{config.agent_name}* not found.")
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
    async with runtime.sessionmaker() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_uuid)
    if store_id is None:
        return None
    return config.agent_name, store_id


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
        resolved = await _resolve_store(
            runtime, team_id=team_id, user_id=user_id, channel_id=channel_id
        )
        if resolved is None:
            text = _EMPTY
        elif not path_arg:
            agent_name, store_id = resolved
            paths: list[str] = []
            page = await runtime.anthropic.beta.memory_stores.memories.list(
                store_id, path_prefix="/"
            )
            async for item in page:
                if item.type == "memory":
                    paths.append(item.path)
            text = (
                _EMPTY
                if not paths
                else _truncate(
                    f"*{agent_name}'s memory* ({len(paths)} files)\n"
                    + "\n".join(f"• `{p}`" for p in sorted(paths))
                )
            )
        else:
            _agent_name, store_id = resolved
            mem_id: str | None = None
            page = await runtime.anthropic.beta.memory_stores.memories.list(
                store_id, path_prefix="/"
            )
            async for item in page:
                if item.type == "memory" and item.path == path_arg:
                    mem_id = item.id
                    break
            if mem_id is None:
                text = f"No memory at `{path_arg}`. Run `/memory` to list paths."
            else:
                mem = await runtime.anthropic.beta.memory_stores.memories.retrieve(
                    mem_id, memory_store_id=store_id, view="full"
                )
                text = _fenced(f"*`{path_arg}`*", mem.content or "", _SLACK_LIMIT)

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
            else "Something went wrong fetching memory — try again later."
        )
        with contextlib.suppress(SlackApiError):
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=error_text,
            )
