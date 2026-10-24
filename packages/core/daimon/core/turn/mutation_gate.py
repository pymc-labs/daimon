"""Live authorization callbacks carried into durable native mutation retries."""

from collections.abc import Awaitable, Callable
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
