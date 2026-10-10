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

    Prepared foreign adoption requires matching admitted profile, native
    resource/tenant/account and a real optional-native usage codec. Its bounded
    replay_usage must reconcile authoritative root/child observations inside
    the admitted persistence lease, retaining journal/claim guarantees. This
    posture alone does not grant provider registration or a verified price.
    The callback supplies authorized dated tariffs and actual infrastructure;
    unknown spend remains pending. Foreign compatibility display counters are
    lower bounds: consumers omit cost without that verified pricing context,
    rather than construct an Anthropic meter from those counters.
    """

    record: ObservationRecorder
