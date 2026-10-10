"""Explicit Agents session controls, verified against the 2026-10-10 reference."""

from typing import Literal

from pydantic import Field

from mux.contracts._base import Contract
from mux.contracts.ids import ResourceRef
from mux.errors import ProviderError


class SessionControls(Contract):
    """Host-selected resource tier and native best-effort session spend limit.

    Omission preserves provider/template defaults. The limit is positive whole
    USD cents across the session, not a per-turn token cap or a final bill.
    """

    model: Literal["gpt-6-luna"] | None = None
    multi_agent_enabled: Literal[False] | None = None
    container_size: Literal["small", "medium", "large"] | None = None
    spend_limit_usd_cents: int | None = Field(
        default=None, gt=0, le=4_503_599_627_370_495, strict=True
    )


class SessionSpendLimitUnverified(ProviderError):
    """Preserve scoped acknowledged resource IDs for exact owned cleanup.

    This is a control failure, never a successful session receipt. No native
    response body, observed cap value or secret is retained in the exception.
    """

    def __init__(self, session: ResourceRef, agent: ResourceRef) -> None:
        super().__init__("permission", retryable=False, native_code="host_spend_limit_unverified")
        self.accepted_session = session
        self.accepted_agent = agent
