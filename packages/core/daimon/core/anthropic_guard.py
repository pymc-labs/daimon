"""Request-scoped refusal of auxiliary Anthropic operations during self-hosted turns."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from uuid import UUID

from anthropic import AsyncAnthropic
from daimon.core.errors import DaimonError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ANTHROPIC_UNCONFIGURED_PLACEHOLDER = "sk-ant-unconfigured-openai-only-deployment"
"""Never a valid credential. `AnthropicSettings.resolved_api_key()` returns
this when no real key is configured, so every `AsyncAnthropic` client keeps
its non-optional constructor; `anthropic_configured` tells the two apart."""

_disabled: ContextVar[bool] = ContextVar("anthropic_disabled", default=False)


class AnthropicDisabled(DaimonError):
    """An optional provider-specific operation is unavailable in this turn."""


class AnthropicUnconfigured(AnthropicDisabled):
    """No Anthropic key on this install, so Managed Agents records are unreachable."""


def anthropic_configured(client: object) -> bool:
    """Whether `client` carries a real key rather than the keyless placeholder.

    A stand-in client that is not an `AsyncAnthropic` (a test double) counts as keyed.
    """
    if client is None:
        return False
    if not isinstance(client, AsyncAnthropic):
        return True
    return client.api_key not in (
        None,
        "",
        ANTHROPIC_UNCONFIGURED_PLACEHOLDER,
    )


def require_anthropic(client: AsyncAnthropic | None) -> None:
    """Refuse Managed Agents I/O in a self-hosted turn or on a keyless install."""
    require_anthropic_allowed()
    if not anthropic_configured(client):
        raise AnthropicUnconfigured("No Anthropic key is configured on this Daimon install.")


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
async def independent_wake_provider_context() -> AsyncIterator[None]:
    """Start a host-owned queued wake outside the draining turn's provider context.

    The dispatcher must immediately resolve the wake's own backend guard.
    Ordinary nested work and MCP calls must keep inheriting their prohibition.
    This changes only provider context, never the caller's thread lock.
    """
    token = _disabled.set(False)
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
    thread_id: str | None,
    caller_id: str | None,
    force: bool = False,
) -> AsyncIterator[bool]:
    """Forbid provider helpers for the whole adapter operation, including child tasks."""
    from daimon.core.channel_backend import effective_thread_backend

    disabled = force or _disabled.get()
    if not disabled:
        async with sessionmaker() as session:
            backend = await effective_thread_backend(
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel_id,
                thread_id=thread_id,
                caller_id=caller_id,
            )
            disabled = backend == "selfhosted"
    with without_anthropic() if disabled else nullcontext():
        yield disabled
