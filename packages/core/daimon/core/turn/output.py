"""Revocable output authorization shared by lifecycle and platform write boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from daimon.core.turn.errors import AdmissionDenied


@dataclass
class OutputAuthorization:
    authorize: Callable[[], Awaitable[None]]
    failed: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(self) -> None:
        async with self._lock:
            if self.failed:
                raise AdmissionDenied(reason="invoker_not_allowed")
            try:
                await self.authorize()
            except asyncio.CancelledError:
                # Cancelling a progress render is not evidence of revoked access.
                # This output stays cancelled; the next output must check again.
                raise
            except AdmissionDenied:
                # Only proved denial persists; transport errors refuse this
                # output and leave later delivery free to check again.
                self.failed = True
                raise

    def close(self) -> None:
        self.failed = True


_CURRENT: ContextVar[OutputAuthorization | None] = ContextVar("output_authorization", default=None)


@contextmanager
def output_authorization(authorization: OutputAuthorization | None) -> Iterator[None]:
    token = _CURRENT.set(authorization)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def output_is_guarded() -> bool:
    return _CURRENT.get() is not None


async def authorize_output() -> None:
    authorization = _CURRENT.get()
    if authorization is not None:
        await authorization.check()


class OutputGate:
    """Keep the same latch on late cleanup and inherited background write tasks."""

    output_authorization: OutputAuthorization | None = None

    def set_output_authorization(self, authorization: OutputAuthorization) -> None:
        self.output_authorization = authorization

    async def check_output(self) -> None:
        if self.output_authorization is not None:
            await self.output_authorization.check()

    def guarded_output[**P, R](self, call: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            with output_authorization(self.output_authorization):
                await authorize_output()
                return await call(*args, **kwargs)

        return guarded
