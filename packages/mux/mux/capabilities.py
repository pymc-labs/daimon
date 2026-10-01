"""Per-backend capability flags.

Callers branch on declared flags instead of assuming every backend behaves
like Anthropic. Flag values in `backends/` are starting positions from the
draft spec, not conclusions — the cross-backend conformance suite adjudicates
them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

BackendId = Literal["anthropic", "openai", "google"]

BACKEND_IDS: tuple[BackendId, ...] = ("anthropic", "openai", "google")


class Capabilities(BaseModel):
    """What one backend can do. Frozen: negotiated once, then read-only."""

    model_config = ConfigDict(frozen=True)

    can_steer: bool
    can_schedule: bool
    durable_fs: bool
    self_hosted_sandbox: bool
