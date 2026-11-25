"""Who pays for embeddings and reranking, and what a payer's balance allows.

Ingest bills the source's tenant; a query bills the asker's. The meter is core's
(`daimon.core.ports.data_metering.SessionMeter`); a library caller that passes none embeds
unmetered, as a script or a test does.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import structlog
from daimon.core.ports.data_metering import Purpose

log = structlog.get_logger()


class Meter(Protocol):
    async def allowed(self, tenant_id: uuid.UUID) -> bool: ...

    def priced(self, model: str) -> bool: ...

    async def record(
        self,
        purpose: Purpose,
        *,
        tenant_id: uuid.UUID,
        platform_user_id: str | None,
        model: str,
        units: int,
    ) -> None: ...


@dataclass(frozen=True)
class Payer:
    """Whose balance a query's embedding and rerank are billed to."""

    tenant_id: uuid.UUID
    platform_user_id: str | None = None


async def can_spend(meter: Meter | None, payer: Payer | None) -> bool:
    """Whether the payer's balance allows model spend now (always, with no meter or payer)."""
    if meter is None or payer is None:
        return True
    return await meter.allowed(payer.tenant_id)
