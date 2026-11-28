"""The failure boundary every scheduler step sits behind."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable

import anthropic
import httpx
import structlog
from daimon.core.channel_backend import backend_refusal_causes
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger(__name__)


def scheduler_step[**P](step: Callable[P, Awaitable[None]]) -> Callable[P, Awaitable[None]]:
    """Mark a sweep or drain that `tick` awaits, and stop its transient failures.

    `_run_loops` ends every loop when one loop raises. A database, Anthropic or
    GitHub error in one step would stop routine dispatch until a restart. This
    boundary logs such an error and returns; the next tick retries. A step may
    keep a narrower catch for its own log event. A test requires every step
    `tick` awaits to carry this decorator or sit in an allow-list.
    """

    @functools.wraps(step)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await step(*args, **kwargs)
        except (SQLAlchemyError, anthropic.APIError, httpx.HTTPError) as error:
            log.exception(
                "scheduler.step.failed", step=step.__name__, causes=backend_refusal_causes(error)
            )

    return guarded
