"""One outcome for a synchronous MCP ask or a fire-and-forget dispatch."""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Concatenate

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.turn.outcomes import current_outcome, observe_turn


def observed_agent_turn[**P, T](
    call: Callable[Concatenate[McpRuntime, AuthIdentity, P], Awaitable[T]],
) -> Callable[Concatenate[McpRuntime, AuthIdentity, P], Awaitable[T]]:
    @wraps(call)
    async def observed(
        runtime: McpRuntime, auth: AuthIdentity, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        # ask invokes start/continue internally; that dispatch is the same turn.
        if current_outcome.get() is not None:
            return await call(runtime, auth, *args, **kwargs)
        with observe_turn(runtime.session_factory, tenant_id=auth.tenant_id, platform="mcp") as row:
            row.account_id = auth.account_id
            row.agent_id = str(auth.agent_id) if auth.agent_id is not None else None
            return await call(runtime, auth, *args, **kwargs)

    return observed
