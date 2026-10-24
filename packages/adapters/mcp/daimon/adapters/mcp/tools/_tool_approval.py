"""Daimon's own Approve card for a gated call on an OpenAI/Gemini turn.

`session_card_gap` hands a provider turn's publish or add_skill call here. The
middleware recorded the exact call (`tool_approvals.pending_call`). A live
approval of that call by its requester in this thread lets it run once.
Otherwise Daimon posts a requester-only Approve card worded like the Claude card
(`prompt_for_tool_call`) and the call stops: nothing runs until the person
presses Approve and the agent calls again with the same arguments.
"""

from __future__ import annotations

import datetime

import anyio
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._session_gate import CardGap
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError

_log = structlog.get_logger()

_UNPOSTED = "Couldn't post the approval card; nothing ran."


async def require_tool_approval(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    *,
    tool_name: str,
) -> CardGap | None:
    """None when this exact call was approved (and is now spent), or is only a preview."""
    from daimon.adapters.mcp.tools._channel_policy import turn_origin_place
    from daimon.adapters.mcp.tools.agents import (
        _private_origin,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.confirmation import prompt_for_tool_call
    from daimon.core.stores import tool_approval_cards as cards
    from daimon.core.switch_cards import SWITCH_CARD_MAX_S
    from daimon.core.tool_approvals import pending_call, requested_approval_card
    from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall

    pending = pending_call.get()
    if pending is None or pending[0] != tool_name:
        # A preview (no gating field): the confirm call asks for the card.
        return None
    _, digest, arguments = pending
    if origin is None:
        return "no_origin"
    if auth.platform != "discord" or auth.platform_user_id is None:
        raise ToolError(
            "I can't post an approval card from this conversation here yet. Nothing ran."
        )
    if _private_origin(origin):
        raise ToolError(
            "I can't post an approval card in a private conversation. Ask in a channel or "
            "thread where I answer. Nothing ran."
        )
    now = datetime.datetime.now(datetime.UTC)
    async with runtime.session_factory.begin() as session:
        spent = await cards.consume(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            thread_id=origin.thread_id,
            tool_name=tool_name,
            args_digest=digest,
            now=now,
        )
    if spent:
        _log.info("tool_approval.spent", tool=tool_name)
        return None
    async with runtime.session_factory() as session:
        waiting = await cards.pending_for(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            tool_name=tool_name,
            args_digest=digest,
            now=now,
        )
    if waiting:
        raise ToolError(
            "There's already an Approve card waiting for this. Once the requester presses "
            "Approve, call this tool again with the same arguments. Nothing ran yet."
        )
    expires_at = now + datetime.timedelta(
        seconds=min(runtime.settings.tool_safety.confirmation_timeout_s, SWITCH_CARD_MAX_S)
    )
    prompt = prompt_for_tool_call(
        ToolCall(
            tool_use_id="daimon-own-card",
            server_name=DAIMON_SERVER_NAME,
            tool_name=tool_name,
            input=arguments,
        ),
        requester_platform_user_id=auth.platform_user_id,
        now=now,
    )
    async with runtime.session_factory.begin() as session:
        row = await cards.create_card(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=origin.platform,
            channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            requester_platform_user_id=auth.platform_user_id,
            tool_name=tool_name,
            args_digest=digest,
            expires_at=expires_at,
        )
    card = requested_approval_card(
        row.id,
        title=prompt.title,
        facts=tuple(fact for fact in (prompt.consequence, *prompt.detail_lines) if fact),
        requester_platform_user_id=auth.platform_user_id,
        expires_at=expires_at,
    )
    from daimon.adapters.mcp.tools.discord._switch_card import post_switch_card

    try:
        message_id = await post_switch_card(
            runtime, auth, channel_id=origin.thread_id, card=card, origin=turn_origin_place(origin)
        )
        async with runtime.session_factory.begin() as session:
            await cards.mark_posted(session, card_id=row.id, message_id=message_id)
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            async with runtime.session_factory.begin() as session:
                await cards.abandon(session, card_id=row.id)
        if not isinstance(exc, Exception):
            raise
        _log.warning(
            "tool_approval.post_failed", card_id=str(row.id), error_type=type(exc).__name__
        )
        raise ToolError(_UNPOSTED) from exc
    raise ToolError(
        "I posted an Approve card in this conversation; nothing ran yet. Tell the requester "
        "to press Approve, then call this tool again with the same arguments."
    )
