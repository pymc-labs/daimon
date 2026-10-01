"""Anthropic backend: the reference surface.

Claude Managed Agents is the most expressive backend (steering, cron,
persistent filesystem) and the surface the draft spec borrows its concepts
from. Daimon runs on it today, so it is also the first Track B consumer.
"""

from __future__ import annotations

from mux.capabilities import Capabilities

BACKEND_ID = "anthropic"

CAPABILITIES = Capabilities(
    can_steer=True,
    can_schedule=True,
    durable_fs=True,
    self_hosted_sandbox=True,
)
