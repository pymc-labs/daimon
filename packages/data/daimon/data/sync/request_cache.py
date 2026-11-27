"""Answers shared by the upstream calls of one request.

A read that re-checks several records, or asks several readers, repeats the same upstream
reads (a token, a collaborator list, a roster). Inside `scope()` the first caller of a key
makes the call and the rest wait for its answer; outside a scope every caller makes its own.
Nothing outlives the scope, and a failed call is shared like an answer.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from contextlib import asynccontextmanager

_cache: contextvars.ContextVar[dict[Hashable, asyncio.Future[object]] | None] = (
    contextvars.ContextVar("request_cache", default=None)
)


@asynccontextmanager
async def scope() -> AsyncIterator[None]:
    held: dict[Hashable, asyncio.Future[object]] = {}
    mark = _cache.set(held)
    try:
        yield
    finally:
        _cache.reset(mark)
        for f in held.values():
            f.cancel()
        await asyncio.gather(*held.values(), return_exceptions=True)


async def once[T](key: Hashable, make: Callable[[], Awaitable[T]]) -> T:
    """What `make()` answers, made once per `key` in this request's scope."""
    held = _cache.get()
    if held is None:
        return await make()
    if key not in held:
        held[key] = asyncio.ensure_future(make())  # pyright: ignore[reportArgumentType]
    return await asyncio.shield(held[key])  # pyright: ignore[reportReturnType]
