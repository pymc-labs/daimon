"""Capture the caller-visible return of an explicitly bound headless invocation.

This boundary is a returned string, not a Discord post, a render snapshot, or
a provider message. The runner must bind ``invoke`` to the real host call and
associate the receipt with its independently captured session/root evidence.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HeadlessOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    boundary: Literal["headless_return"] = "headless_return"
    evidence_id: str = Field(min_length=1)
    started_s: float = Field(ge=0)
    returned_s: float = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def ordered_clock(self) -> HeadlessOutput:
        if self.returned_s < self.started_s:
            raise ValueError("headless return predates invocation")
        return self

    @property
    def has_visible_text(self) -> bool:
        # A successful tool-only turn can return an empty string. It has closed
        # the output window but has not delivered a visible message.
        return bool(self.text.strip())


async def capture_headless_return(
    invoke: Callable[[], Awaitable[str]],
    *,
    evidence_id: str,
    clock: Callable[[], float] = time.monotonic,
) -> HeadlessOutput:
    """Await the actual host return; propagate failure/cancellation without a receipt.

    Capture the string exactly as returned, including the host's truncation.
    Do not accept an on_state/render callback as the invocation, extract text
    from native events, or use this receipt for platform delivery/card coverage.
    A closed receipt covers this single return boundary only. The supplied clock
    must be the same monotonic clock as the runner's turn evidence.
    """
    # Validate identity and the opening clock before invoking the host.
    started = clock()
    # A same-time empty receipt validates the opening fields without dispatch.
    HeadlessOutput(evidence_id=evidence_id, started_s=started, returned_s=started, text="")
    text = await invoke()
    return HeadlessOutput(evidence_id=evidence_id, started_s=started, returned_s=clock(), text=text)
