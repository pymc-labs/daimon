"""Explicit Agents session controls, verified against the 2026-10-10 reference."""

from typing import Literal

from pydantic import Field

from mux.contracts._base import Contract


class SessionControls(Contract):
    """Host-selected resource tier and native best-effort session spend limit.

    Omission preserves provider/template defaults. The limit is positive whole
    USD cents across the session, not a per-turn token cap or a final bill.
    """

    container_size: Literal["small", "medium", "large"] | None = None
    spend_limit_usd_cents: int | None = Field(default=None, gt=0, strict=True)
