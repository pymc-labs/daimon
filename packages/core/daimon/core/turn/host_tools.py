"""Custom tools the host runs for a session: their calls are answered by the turn itself.

Off unless a caller sets `host_tools` around a turn. Eval items run Daimon's tools this way,
so the provider never dials a server. A call to a tool that `ask`s is decided as one the
provider paused on: by a `PolicyApproval` turn's decider, or refused under `RequireApproval`,
which has no card for it. A refused call gets the refusal as its result and never runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import cast

from anthropic.types.beta.sessions import BetaManagedAgentsEventParams
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.posture import PolicyApproval, RequireApproval, ToolConfirmation
from daimon.core.turn.state import ToolUseBlock, TurnState

#: One call's result: its text and whether it failed.
HostToolCall = Callable[[str, Mapping[str, object]], Awaitable[tuple[str, bool]]]


@dataclass(frozen=True)
class HostTool:
    #: The MCP server the tool stands for, as tool rules name it.
    server: str
    call: HostToolCall
    #: Whether the provider would have paused on it (`always_ask`).
    ask: bool = False


_DECLINED = "The call was declined. Nothing was done."
#: Tools by name the running turn answers; None (the default) answers none.
host_tools: ContextVar[Mapping[str, HostTool] | None] = ContextVar("host_tools", default=None)


def _host_blocks(state: TurnState, tools: Mapping[str, HostTool]) -> dict[str, ToolUseBlock]:
    return {
        b.id: b for b in state.content
        if isinstance(b, ToolUseBlock) and b.type == "agent.custom_tool_use" and b.name in tools
    }  # fmt: skip


def only_host_calls(state: TurnState, pending: Sequence[str]) -> bool:
    """Whether every call a pause names is the host's to answer."""
    blocks = _host_blocks(state, host_tools.get() or {})
    return bool(pending) and all(i in blocks for i in pending)


async def answer_host_tools(
    state: TurnState,
    pending: Sequence[str],
    *,
    answered: set[str],
    posture: ToolConfirmation,
) -> list[BetaManagedAgentsEventParams]:
    """`user.custom_tool_result` events for the host's calls among `pending` not yet in
    `answered`, which it extends. Empty when none are the host's."""
    tools = host_tools.get() or {}
    blocks = _host_blocks(state, tools)
    ids = [i for i in pending if i in blocks and i not in answered]
    answered.update(ids)

    async def one(block: ToolUseBlock) -> BetaManagedAgentsEventParams:
        tool = tools[block.name]
        text, failed = "", False
        if tool.ask and isinstance(posture, PolicyApproval):
            call = ToolCall(tool_use_id=block.id, server_name=tool.server, tool_name=block.name,
                            input=dict(block.input))  # fmt: skip
            verdict = await posture.decide(call)
            if not verdict.allow:
                text, failed = verdict.deny_message or _DECLINED, True
        elif tool.ask and isinstance(posture, RequireApproval):
            text, failed = _DECLINED, True
        if not failed:
            text, failed = await tool.call(block.name, block.input)
        return cast(BetaManagedAgentsEventParams, {
            "type": "user.custom_tool_result", "custom_tool_use_id": block.id,
            "content": [{"type": "text", "text": text}], "is_error": failed,
        })  # fmt: skip

    return list(await asyncio.gather(*(one(blocks[i]) for i in ids)))
