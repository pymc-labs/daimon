"""Live authorization callbacks carried into durable native mutation retries."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

unattended_execution: ContextVar[bool] = ContextVar("turn_unattended_execution", default=False)

unattended_gate: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "turn_unattended_gate", default=None
)
initial_send_check: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "turn_initial_send_check", default=None
)


async def recheck_retry() -> None:
    """Refresh source/spend and unattended capabilities after a proven refusal."""
    if (check := initial_send_check.get()) is not None:
        await check()
    if (gate := unattended_gate.get()) is not None:
        await gate()


#: Text the initial send puts first, built once inside the turn's lease
#: (`daimon.core.host_calls.confirmed_call_prefix`).
initial_send_prefix: ContextVar[Callable[[], Awaitable[str]] | None] = ContextVar(
    "turn_initial_send_prefix", default=None
)


@asynccontextmanager
async def send_prefix(prefix: Callable[[], Awaitable[str]] | None) -> AsyncIterator[None]:
    """`initial_send_prefix` for the turns run inside; nothing when `prefix` is None."""
    if prefix is None:
        yield
        return
    token = initial_send_prefix.set(prefix)
    try:
        yield
    finally:
        initial_send_prefix.reset(token)
