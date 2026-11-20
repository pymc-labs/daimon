"""Revocable output authorization shared by lifecycle and platform write boundaries."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from daimon.core.access_policy import place_thread_id
from daimon.core.audience_gate import (
    AUDIENCE_CONTAINED,
    GATED_PLATFORMS,
    log_shadow_refusal,
    read_audience_mode,
)
from daimon.core.audience_refusal import audience_refusal
from daimon.core.channel_audience import AudienceMode, channel_has_outsiders
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_hold import TurnHold
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
    #: The room-audience latch (`audience_hold`), kept apart from `output_authorization`: that
    #: one restricts a watch's or direct message's output, and detached file sweeps read it as
    #: such. This one never does.
    audience_latch: OutputAuthorization | None = None

    def set_output_authorization(self, authorization: OutputAuthorization) -> None:
        self.output_authorization = authorization

    def set_audience_latch(self, latch: OutputAuthorization) -> None:
        self.audience_latch = latch

    def _combined(self) -> OutputAuthorization | None:
        first, second = self.output_authorization, self.audience_latch
        if second is None:
            return first
        if first is None:
            return second

        async def both() -> None:
            await first.check()
            await second.check()

        return OutputAuthorization(both)

    async def check_output(self) -> None:
        if self.output_authorization is not None:
            await self.output_authorization.check()
        if self.audience_latch is not None:
            await self.audience_latch.check()

    def guarded_output[**P, R](self, call: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            with output_authorization(self._combined()):
                await authorize_output()
                return await call(*args, **kwargs)

        return guarded


def audience_output_check(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
    hold: TurnHold | None,
    agent: str | None,
    mode: AudienceMode | None = None,
) -> Callable[[], Awaitable[None]] | None:
    """Gate A: the check a turn's output latch runs before each post and edit in its room.

    None when there is nothing to check: a platform with no `audience_hold`, or a turn
    with no room (a direct message, the web app). `mode` is the one the turn was admitted
    under (`Admission.audience_mode`); with None it is read once, at the first output, and a
    failed read raises and that output is refused. In `shadow` a fact that cannot be read is
    logged and the output goes ahead; in `on` it refuses. A refusal is `AdmissionDenied`,
    which the latch keeps for the rest of the turn.
    """
    if platform not in GATED_PLATFORMS or channel_id is None:
        return None
    thread = place_thread_id(thread_id)
    thread = None if thread == channel_id else thread
    destination = thread or channel_id
    parent = channel_id if thread else None
    state: dict[str, AudienceMode | bool] = {} if mode is None else {"mode": mode}

    async def check() -> None:
        mode = state.get("mode")
        if mode is None:
            mode = state["mode"] = await read_audience_mode(
                sessionmaker,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=destination,
                parent_id=parent,
            )
        if mode == "off":
            return
        try:
            async with sessionmaker() as session:
                fact = await channel_has_outsiders(
                    session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
                )
        except Exception as exc:
            if mode != "shadow":
                raise
            log_shadow_refusal(
                tenant_id=tenant_id,
                channel_id=destination,
                agent=agent,
                reason=type(exc).__name__,
                kind="fact_unreadable",
            )
            return
        if audience_refusal(fact, hold, destination, parent) is None:
            return
        if mode == "shadow":
            if not state.get("logged"):
                state["logged"] = True
                log_shadow_refusal(tenant_id=tenant_id, channel_id=destination, agent=agent)
            return
        raise AdmissionDenied(reason="invoker_not_allowed", detail=AUDIENCE_CONTAINED)

    return check
