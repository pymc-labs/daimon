"""A channel named across tenants and platforms. Pure."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.stores.domain import Platform


@dataclass(frozen=True)
class ChannelRef:
    """One channel (or a thread under it) in one tenant: a Space member, a delivery target."""

    tenant_id: uuid.UUID
    platform: Platform
    channel_id: str
    thread_id: str | None = None

    def key(self) -> str:
        """A stable string for maps and logs; distinct across tenants and platforms."""
        thread = f"/{self.thread_id}" if self.thread_id is not None else ""
        return f"{self.platform}:{self.tenant_id}:{self.channel_id}{thread}"
