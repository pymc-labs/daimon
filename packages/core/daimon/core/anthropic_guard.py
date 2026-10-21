"""Request-scoped refusal of auxiliary Anthropic operations during self-hosted turns."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from uuid import UUID

from daimon.core.errors import DaimonError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_disabled: ContextVar[bool] = ContextVar("anthropic_disabled", default=False)


class AnthropicDisabled(DaimonError):
    """An optional provider-specific operation is unavailable in this turn."""


def anthropic_disabled() -> bool:
    """Whether the current operation forbids Anthropic I/O."""
    return _disabled.get()


def require_anthropic_allowed() -> None:
    if _disabled.get():
        raise AnthropicDisabled("This operation is unavailable during a self-hosted turn.")


@contextmanager
def without_anthropic() -> Iterator[None]:
    """Inherited by child tasks; restored independently for each concurrent request."""
    token = _disabled.set(True)
    try:
        yield
    finally:
        _disabled.reset(token)


@asynccontextmanager
async def guard_selfhosted_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None = None,
    caller_id: str | None = None,
    force: bool = False,
) -> AsyncIterator[bool]:
    """Forbid provider helpers for the whole adapter operation, including child tasks."""
    from daimon.core.channel_backend import is_selfhosted_turn

    disabled = force or _disabled.get()
    if not disabled:
        async with sessionmaker() as session:
            disabled = await is_selfhosted_turn(
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel_id,
                thread_id=thread_id,
                caller_id=caller_id,
            )
    with without_anthropic() if disabled else nullcontext():
        yield disabled
