"""One injectable monotonic clock and sequence for an offline host executor."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict, Field

_HOST_CLOCK: ContextVar[HostClock | None] = ContextVar("offline_host_clock", default=None)


class ClockSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    clock_id: str = Field(min_length=1)
    observed_s: float = Field(ge=0)
    order: int = Field(ge=1)


class HostClock:
    """Share this callable with root, output and platform capture boundaries.

    Time samples come only from the supplied clock, never a catalog deadline.
    A single executor-wide sequence preserves observation order at equal times.
    Neither a clock id nor a serialized sample supplies native root authority.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._id = uuid.uuid4().hex
        self._last: ClockSample | None = None

    def sample(self) -> ClockSample:
        previous = self._last
        sample = ClockSample(
            clock_id=self._id,
            observed_s=self._clock(),
            order=1 if previous is None else previous.order + 1,
        )
        if previous is not None and sample.observed_s < previous.observed_s:
            raise ValueError("host clock must be monotonic")
        self._last = sample
        return sample

    def __call__(self) -> float:
        return self.sample().observed_s

    @property
    def last(self) -> ClockSample:
        if self._last is None:
            raise ValueError("host clock has no observation")
        return self._last


def current_host_clock() -> Callable[[], float]:
    """Read the executor clock; ordinary callers retain monotonic time."""
    return _HOST_CLOCK.get() or time.monotonic


@contextmanager
def use_host_clock(clock: HostClock) -> Iterator[None]:
    """Inject one clock through async host children, restoring caller context."""
    token = _HOST_CLOCK.set(clock)
    try:
        yield
    finally:
        _HOST_CLOCK.reset(token)
