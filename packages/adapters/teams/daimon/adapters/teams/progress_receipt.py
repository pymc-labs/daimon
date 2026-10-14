"""Opt-in facts from an accepted initial adapter card, before native binding.

The rendered input is not a wire or running-root certificate. Consumers must
join it to independent accepted wire posts and subsequent root/binding evidence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InitialProgressReceipt(BaseModel):
    """A real post_initial send/edit ACK; no provider/root identity is invented."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    platform: Literal["teams"] = "teams"
    phase: Literal["initial"] = "initial"
    operation: Literal["send", "edit"]
    destination_id: str = Field(min_length=1)
    thread_id: str | None = None
    message_id: str = Field(min_length=1)
    rendered_json: str = Field(min_length=1)
    observed_s: float = Field(ge=0)
