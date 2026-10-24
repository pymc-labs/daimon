"""Switch this conversation's model when someone asks in words ("use opus", "think harder").

``register_thread_model_tools(mcp, runtime)`` wires the ``@mcp.tool`` closure;
it delegates to ``_set_thread_model_impl`` so the gate is testable without a
FastMCP Context. ``/model`` on Discord writes the same setting
(`daimon.core.thread_models`).

Only a person's own turn may switch it: the call must name this execution's
verified origin, and that origin must be attended. Routines, wakes and
agent-chat callers have no attended origin, so they can never change the model
a conversation's people chose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.thread_models import (
    FLEET_ROOT_NOTE,
    ThreadModelRefused,
    choose_thread_model,
    thread_runs_fleet,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_UNATTENDED = (
    "Only someone in the conversation can change its model, and nobody started "
    "this run. Nothing was changed."
)


@dataclass(frozen=True)
class ThreadModelResult:
    """Result returned from set_thread_model."""

    model: str
    """The model id now chosen, or 'default' for the agent's own."""
    note: str
    """The sentence to report back to the person."""


async def _set_thread_model_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    model: str,
) -> ThreadModelResult:
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if not origin.attended:
        raise ToolError(_UNATTENDED)
    try:
        change = await choose_thread_model(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            choice=model,
        )
    except ThreadModelRefused as exc:
        raise ToolError(str(exc)) from exc
    note = change.note
    if await thread_runs_fleet(
        runtime.session_factory,
        fleet_enabled=runtime.settings.fleet.enabled,
        tenant_id=auth.tenant_id,
        platform=origin.platform,
        thread_id=origin.thread_id,
        ma_agent_id=origin.responder_ma_agent_id,
        now=datetime.now(UTC),
    ):
        note += FLEET_ROOT_NOTE
    return ThreadModelResult(model=change.model_id or "default", note=note)


def register_thread_model_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def set_thread_model(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        model: Literal["haiku", "sonnet", "opus", "default"],
    ) -> ThreadModelResult:
        """Run this conversation on another model when the person asks in words: "use opus here", "think harder".

        Only on the person's direct instruction in this conversation, never on
        your own judgement or because a model is mentioned. "Think harder" or
        "think as hard as you can" means ``opus``; ``default`` goes back to the
        agent's own model. There is no effort setting. The conversation keeps
        the choice for every later reply until someone changes it; the reply
        in progress keeps its model. Other conversations are unaffected, and
        a worker fleet (its root and every worker) always runs the agent's
        approved model.

        ``origin_context_id`` must name this execution's attended origin;
        routines and other unattended runs are refused. Read the returned
        ``note`` back to the person.
        """  # noqa: E501
        return await _set_thread_model_impl(
            runtime, await _auth(ctx), origin_context_id=origin_context_id, model=model
        )
