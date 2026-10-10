"""Billing posture for authoritative provider usage, independent of display events."""

from dataclasses import dataclass
from typing import Protocol

from mux.contracts.usage import UsageObservation


class ObservationRecorder(Protocol):
    async def __call__(self, *, observation: UsageObservation) -> bool | None: ...


@dataclass(frozen=True)
class ObservationBilled:
    """The callback binds tenant, attribution, grain and verified price context.

    False means pending/unverified; the host must permit post-run retries of
    the same revision. An exception is a fail-closed accounting failure.
    The recorder owns outcome capture after authorized model attribution;
    record_provider_usage supplies both accounting and outcome recording.
    """

    record: ObservationRecorder
